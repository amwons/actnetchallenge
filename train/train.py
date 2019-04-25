import sys, os
import time
import shutil
import argparse

import numpy as np
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.nn.utils.rnn import pack_padded_sequence
import torch.optim as optim

sys.path.append(os.pardir)
from utils.utils import *
from langmodels.lstm import RNNCaptioning
from langmodels.vocab import Vocabulary
from imagemodels.resnet import resnet10, resnet18, resnet34, resnet50, resnet101, resnet152, resnet200
from dataset.activitynet_captions import ActivityNetCaptions
import transforms.spatial_transforms as spt
import transforms.temporal_transforms as tpt

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--root_path', type=str, default='../../../ssd1/dsets/activitynet_captions')
    parser.add_argument('--model_path', type=str, default='../models')
    parser.add_argument('--meta_path', type=str, default='videometa_train.json')
    parser.add_argument('--mode', type=str, default='train')
    parser.add_argument('--framepath', type=str, default='frames')
    parser.add_argument('--annpath', type=str, default='train.json')
    parser.add_argument('--cnnmethod', type=str, default='resnet')
    parser.add_argument('--rnnmethod', type=str, default='LSTM')
    parser.add_argument('--vocabpath', type=str, default='vocab.json')
    parser.add_argument('--start_from_ep', type=int, default=0)
    parser.add_argument('--log_every', type=int, default=10)
    parser.add_argument('--num_layers', type=int, default=10)
    parser.add_argument('--imsize', type=int, default=224)
    parser.add_argument('--clip_len', type=int, default=16)
    parser.add_argument('--bs', type=int, default=64)
    parser.add_argument('--n_cpu', type=int, default=8)
    parser.add_argument('--lstm_memory', type=int, default=512)
    parser.add_argument('--embedding_size', type=int, default=512)
    parser.add_argument('--max_seqlen', type=int, default=30)
    parser.add_argument('--max_epochs', type=int, default=20)
    parser.add_argument('--lr', type=float, default=1e-2)
    parser.add_argument('--momentum', type=int, default=0.9)
    parser.add_argument('--patience', type=int, default=10)
    parser.add_argument('--lstm_stacks', type=int, default=3)
    parser.add_argument('--token_level', action='store_true')
    parser.add_argument('--cuda', action='store_false')
    parser.add_argument('--dataparallel', action='store_false')
    args = parser.parse_args()


    # gpus
    device = torch.device('cuda' if args.cuda and torch.cuda.is_available() else 'cpu')
    n_gpu = torch.cuda.device_count()
    print("using {} gpus...".format(n_gpu))

    # load vocabulary
    vocab = Vocabulary(token_level=args.token_level)
    vocpath = os.path.join(args.root_path, args.vocabpath)
    if not os.path.exists(vocpath):
        vocab.add_corpus(os.path.join(args.root_path, args.annpath))
        vocab.save(vocpath)
    else:
        vocab.load(vocpath)
    vocab_size = len(vocab)

    # transforms
    sp = spt.Compose([spt.CornerCrop(size=args.imsize), spt.ToTensor()])
    tp = tpt.Compose([tpt.TemporalRandomCrop(args.clip_len), tpt.LoopPadding(args.clip_len)])

    # dataloading
    train_dset = ActivityNetCaptions(args.root_path, args.meta_path, args.mode, vocab, args.framepath, spatial_transform=sp, temporal_transform=tp)
    trainloader = DataLoader(train_dset, batch_size=args.bs, shuffle=True, num_workers=args.n_cpu, collate_fn=collater, drop_last=True)
    max_it = int(len(train_dset) / args.bs)

    # models
    video_encoder = resnet10(sample_size=args.imsize, sample_duration=args.clip_len)
    caption_gen = RNNCaptioning(method=args.rnnmethod, emb_size=args.embedding_size, lstm_memory=args.lstm_memory, vocab_size=vocab_size, max_seqlen=args.max_seqlen, num_layers=args.lstm_stacks)
    models = [video_encoder, caption_gen]

    # apply pretrained model
    offset = args.start_from_ep
    if offset != 0:
        enc_model_dir = os.path.join(args.model_path, "{}_{}".format(args.cnnmethod, args.num_layers), "b{:03d}_s{:03d}_l{:03d}".format(args.bs, args.imsize, args.clip_len))
        enc_filename = "ep{:04d}.ckpt".format(offset)
        enc_model_path = os.path.join(enc_model_dir, enc_filename)
        dec_model_dir = os.path.join(args.model_path, "{}_{}".format(args.rnnmethod, args.lstm_stacks), "b{:03d}_s{:03d}_l{:03d}".format(args.bs, args.imsize, args.clip_len))
        dec_filename = "ep{:04d}.ckpt".format(offset)
        dec_model_path = os.path.join(dec_model_dir, dec_filename)
        if os.path.exists(enc_model_path) and os.path.exists(dec_model_path):
            video_encoder.load_state_dict(torch.load(enc_model_path))
            caption_gen.load_state_dict(torch.load(dec_model_path))
            print("restarting training from epoch {}".format(offset))
        else:
            offset = 0
            print("didn't find file, starting encoder from scratch")

    # move models to device
    video_encoder = video_encoder.to(device)
    caption_gen = caption_gen.to(device)
    if n_gpu > 1 and args.dataparallel:
        video_encoder = nn.DataParallel(video_encoder)
        caption_gen = nn.DataParallel(caption_gen)

    # loss function
    criterion = nn.CrossEntropyLoss()

    # optimizer, scheduler
    params = list(video_encoder.parameters()) + list(caption_gen.parameters())
    optimizer = optim.SGD(params, lr=args.lr, momentum=args.momentum)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=0.5, patience=args.patience)

    # count parameters
    num_params = sum(count_parameters(model) for model in models)
    print("# of params in model : {}".format(num_params))

    # training loop
    assert args.max_epochs > offset, "already at offset epoch number, aborting training"
    print("start training")
    before = time.time()
    for ep in range(offset, args.max_epochs):
        for it, data in enumerate(trainloader):

            clip, captions, lengths = data

            optimizer.zero_grad()

            # move to device
            clip = clip.to(device)
            captions = captions.to(device)
            lengths = lengths.to(device)

            # flow through model
            feature = video_encoder(clip)
            feature = feature.view(args.bs, args.embedding_size)
            caption = caption_gen(feature, captions, lengths)

            # move targets one ahead of input captions
            tmp = torch.zeros(args.bs, 1, dtype=torch.long).to(device)
            targets = torch.cat((captions[:, 1:], tmp), dim=1)
            targets = pack_padded_sequence(targets, lengths, batch_first=True)[0]

            nll = criterion(caption, targets)
            nll.backward()
            optimizer.step()

            # log losses
            if it % args.log_every == (args.log_every-1):
                after = time.time()
                print("iter {:06d}/{:06d} | nll loss: {:.04f} | {:02.04f}s per loop".format(it+1, max_it, nll.cpu().item(), (after-before)/args.log_every), flush=True)
                before = time.time()

        scheduler.step(nll.cpu().item())
        print("epoch {:04d}/{:04d} done, loss: {:.06f}".format(ep+1, args.max_epochs, nll.cpu().item()), flush=True)

        # save models
        enc_save_dir = os.path.join(args.model_path, "{}_{}".format(args.cnnmethod, args.num_layers), "b{:03d}_s{:03d}_l{:03d}".format(args.bs, args.imsize, args.clip_len))
        enc_filename = "ep{:04d}.ckpt".format(ep+1)
        enc_save_path = os.path.join(enc_save_dir, enc_filename)
        dec_save_dir = os.path.join(args.model_path, "{}_{}".format(args.rnnmethod, args.lstm_stacks), "b{:03d}_s{:03d}_l{:03d}".format(args.bs, args.imsize, args.clip_len))
        dec_filename = "ep{:04d}.ckpt".format(ep+1)
        dec_save_path = os.path.join(dec_save_dir, dec_filename)
        if not os.path.exists(enc_save_dir):
            os.makedirs(enc_save_dir)
        if not os.path.exists(dec_save_dir):
            os.makedirs(dec_save_dir)

        torch.save(video_encoder.module.state_dict(), enc_save_path)
        print("saved encoder model to {}".format(enc_save_path))
        torch.save(caption_gen.module.state_dict(), dec_save_path)
        print("saved decoder model to {}".format(dec_save_path))

        before = time.time()


    print("end training")





