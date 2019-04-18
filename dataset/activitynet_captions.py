import sys, os
import time

from PIL import Image
import functools
import json

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from torchvision import transforms


# video loader.
# loads frame indices and gets
def video_loader(video_dir_path, frame_indices, image_loader):
    video = []
    for i in frame_indices:
        image_path = os.path.join(video_dir_path, '{:06d}.jpg'.format(i))
        if os.path.exists(image_path):
            video.append(image_loader(image_path))
        else:
            return video

    return video

# 
def get_default_video_loader():
    image_loader = get_default_image_loader()
    return functools.partial(video_loader, image_loader=image_loader)

# get default image loader. accimage is faster than PIL
def get_default_image_loader():
    from torchvision import get_image_backend
    if get_image_backend() == 'accimage':
        return accimage_loader
    else:
        return pil_loader

def pil_loader(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, 'rb') as f:
        with Image.open(f) as img:
            return img.convert('RGB')

def accimage_loader(path):
    try:
        import accimage
        return accimage.Image(path)
    except IOError:
        # Potentially a decoding problem, fall back to PIL.Image
        return pil_loader(path)

def id_loader(path):
    """
    Args:
        path : string containing path to json file containing video ids
    Returns:
        (id2key, key2id) where:
            id2key : list containing video ids
            key2id : dictionary containing mapping of video ids to indexes
    """
    key2id = {}
    with open(path, "r") as f:
        ids = json.load(f)
    # remove "v_" prefix
    id2key = [id[2:] for id in ids]
    for index, videoid in enumerate(id2key):
        key2id[videoid] = index
    return id2key, key2id

"""
preprocess object from activitynet captions dataset.
concats metadata into data.
data attributes: list of dict. list index is the index of video, dict is info about video.
dict : {
    'video_id'      : str, shows the video id.
    'framerate'     : float, shows the framerate per second
    'num_frames'    : int, total number of frames in the video
    'width'         : int, the width of video in pixels
    'height'        : int, the height of video in pixels
    'regions'       : [[int, int], [int, int], ...], list including start and end frames of actions
    'captions'      : [str, str, ...], list including captions for each action
    'segments'      : int, number of actions
}
"""
def preprocess(predata, metadata, vidnum, key2idx):
    data = [None] * vidnum
    tmp = {}
    for obj in metadata:
        idx = key2idx[obj['video_id']]
        tmp['video_id'] = obj['video_id']
        tmp['framerate'] = obj['framerate']
        tmp['num_frames'] = obj['num_frames']
        tmp['width'] = obj['width']
        tmp['height'] = obj['height']
        data[idx] = tmp
    for v_id, obj in predata.items():
        try:
            idx = key2idx[obj['video_id']]
        except KeyError:
            continue
        fps = data[idx]['framerate']
        regions_sec = obj['timestamps']
        captions = obj['captions']
        regs = []
        regcnt = 0
        for region in regions_sec:
            # convert into frame duration
            region = [int(ts*fps) for ts in region]
            regs.append(region)
            regcnt += 1
        data[idx]['regions'] = regs
        data[idx]['captions'] = captions
        data[idx]['segments'] = regcnt
    return data


class ActivityNetCaptions(Dataset):
    """
    Args:
        root_path (string): Root directory path.
        mode : 'train', 'val', 'test'
        spatial_transform (callable, optional): A function/transform that takes in a PIL image
            and returns a transformed version. E.g, ``transforms.RandomCrop``
        temporal_transform (callable, optional): A function/transform that takes in a list of frame indices
            and returns a transformed version
        target_transform (callable, optional): A function/transform that takes in the
            target and transforms it.
        loader (callable, optional): A function to load a video given its path and frame indices.
     Attributes:
        classes (list): List of the class names.
        class_to_idx (dict): Dict with items (class_name, class_index).
        imgs (list): List of (image path, class_index) tuples
    """

    def __init__(self,
                 root_path,
                 metadata,
                 mode,
                 frame_path='frames',
                 is_adaptively_dilated=False,
                 n_samples_for_each_video=1,
                 spatial_transform=None,
                 temporal_transform=None,
                 target_transform=None,
                 sample_duration=16,
                 get_loader=get_default_video_loader):

        self.dilate = is_adaptively_dilated

        idpath = "{}_ids.json".format(mode)
        self.idx2key, self.key2idx = id_loader(os.path.join(root_path, idpath))

        try:
            assert mode in ['train', 'val', 'test']
        except AssertionError:
            print("mode in ActivityNetCaptions must be one of ['train', 'val', 'test']", True)
        self.category = 'val_1' if mode is 'val' else mode
        self.annfile = "{}.json".format(self.category) if mode is not 'test' else None

        # load annotation files
        if self.annfile is not None:
            with open(os.path.join(root_path, self.annfile)) as f:
                self.predata = json.load(f)

        # load metadata files
        with open(os.path.join(root_path, metadata)) as f:
            self.meta = json.loads(f.read())

        # save frame root path
        self.frame_path = os.path.join(root_path, frame_path)

        self.vidnum = len(self.meta)
        self.data = preprocess(self.predata, self.meta, self.vidnum, self.key2idx)
        print(self.data)
        self.spatial_transform = spatial_transform
        self.temporal_transform = temporal_transform
        self.target_transform = target_transform
        self.loader = get_loader

    def __getitem__(self, index):
        """
        Args:
            index (int): Index
        Returns:
            tuple: (image, segments) where target is class_index of the target class.
        """

        """
        preprocess object from activitynet captions dataset.
        concats metadata into data.
        data attributes: list of dict. list index is the index of video, dict is info about video.
        dict : {
            'video_id'      : str, shows the video id.
            'framerate'     : float, shows the framerate per second
            'num_frames'    : int, total number of frames in the video
            'width'         : int, the width of video in pixels
            'height'        : int, the height of video in pixels
            'regions'       : [[int, int], [int, int], ...], list including start and end frames of actions
            'captions'      : [str, str, ...], list including captions for each action
            'segments'      : int, number of actions
        }
        """
        id = self.data[index]['video_id']
        num_frames = self.data[index]['num_frames']

        frame_indices = list(range(num_frames))

        if self.temporal_transform is not None:
            frame_indices = self.temporal_transform(frame_indices)
        clip = self.loader(path, frame_indices)
        if self.spatial_transform is not None:
            self.spatial_transform.randomize_parameters()
            clip = [self.spatial_transform(img) for img in clip]
        clip = torch.stack(clip, 0).permute(1, 0, 2, 3)

        target = self.data[index]
        if self.target_transform is not None:
            target = self.target_transform(target)

        return clip, target

    def __len__(self):
        return self.vidnum


if __name__ == '__main__':
    dset = ActivityNetCaptions('../../../ssd1/dsets/activitynet_captions', 'videometa_train.json', 'train', 'frames')
