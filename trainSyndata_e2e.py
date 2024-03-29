import os
import yaml
import sys
import torch
import torch.utils.data as data

import cv2
import os.path as osp
import time
import numpy as np
from utils import normalize_transforms
import scipy.io as scio
import argparse
import time
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torchutil import AverageMeter, create_logger
import torch.optim as optim
import random
import h5py
import re
import skimage.transform as trans
import water
from test import test


from math import exp
from e2e_data_loader import ICDAR2015, Synth80k, ICDAR2013
from e2e_data_loader import my_collate, get_region, resize_pad

###import file#######
from mseloss import Maploss



from collections import OrderedDict
from eval.script import getresult



from PIL import Image
from torchvision.transforms import transforms
from craft_e2e import CRAFT
from torch.autograd import Variable
from multiprocessing import Pool
import os
from utils import CTCLabelConverter, AttnLabelConverter
from recognition_model import Model
from imgproc import denormalizeMeanVariance
#3.2768e-5
random.seed(42)

def mkdirs(dir):
    if not osp.exists(dir):
        os.mkdir(dir)

def str2bool(v):
    return v.lower() in ("yes", "y", "true", "t", "1")



# class SynAnnotationTransform(object):
#     def __init__(self):
#         pass
#     def __call__(self, gt):
#         image_name = gt['imnames'][0]


def copyStateDict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = ".".join(k.split(".")[start_idx:])
        new_state_dict[name] = v
    return new_state_dict

def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (0.8 ** step)
    print(lr)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def generate_recognition_model(args):
    if 'CTC' in args.Prediction:
        converter = CTCLabelConverter(args.character)
    else:
        converter = AttnLabelConverter(args.character)
    args.num_class = len(converter.character)

    if args.rgb:
        args.input_channel = 3
    model = Model(args)
    if 'CTC' in args.Prediction:
        criterion = torch.nn.CrossEntropyLoss(ignore_index=0).cuda()
    else:
        criterion = torch.nn.CrossEntropyLoss(ignore_index=0).cuda()

    filtered_parameters = []
    params_num = []
    for p in filter(lambda p: p.requires_grad, model.parameters()):
        filtered_parameters.append(p)
        params_num.append(np.prod(p.size()))
    print('Trainable params num: {}'.format(sum(params_num)))
    optimizer = torch.optim.Adadelta(filtered_parameters, lr = args.reco_lr, rho=0.95, eps=0.999)
    return model, converter, criterion, optimizer


def left_point(x):
    return x[0]

def get_region_torch(img, bbox):
    img = img.unsqueeze(0)
    bbox = [i/4. for i in bbox]
    bbox.sort(key=left_point)
    if bbox[0][1] < bbox[1][1]:
        left_top = bbox[0]
        left_bottom = bbox[1]
    else:
        left_top = bbox[1]
        left_bottom = bbox[0]
    if bbox[2][1] < bbox[3][1]:
        right_top = bbox[2]
        right_bottom = bbox[3]
    else:
        right_top = bbox[3]
        right_bottom = bbox[2]
    w = np.linalg.norm(np.float32(right_top) - np.float32(left_top))
    h = np.linalg.norm(np.float32(left_bottom) - np.float32(left_top))

    width = w
    height = h
    if h> w*1.5:
        width = h
        height = w

        src = np.float32([left_top, right_top, right_bottom])
        dst = np.float32([[0,0], [0, height], [height, width]])
    else:
        src = np.float32([left_top, right_top, right_bottom])
        dst = np.float32([[0,0], [0, width], [height, width]])
    tr = trans.estimate_transform('affine', src=src, dst=dst)
    inv_tr = trans.estimate_transform('affine', src=dst, dst=src)
    img_torch = img

    w1, h1 = img_torch.size()[2:]
    param = np.linalg.inv(tr.params)
    theta = normalize_transforms(param[0:2, :], w1, h1)
    inv_param = np.linalg.inv(inv_tr.params)
    inv_theta = normalize_transforms(inv_param[0:2, :], w1, h1)
    theta = torch.from_numpy(theta).float()
    N, C, W, H = img_torch.size()
    size = torch.Size((N, C, W, H))
    grid = F.affine_grid(theta.unsqueeze(0), size)
    output = F.grid_sample(img_torch.cpu(), grid)
    new_img_torch = output[:, :, :int(height)+1,:int(width)+1]
    return new_img_torch



def resize_pad(bbox, height = 16, max_pad = 200):
    h,w = bbox[2:]
    width = height/h * w
    width = int(width)
    bbox = torch.nn.functional.upsample(bbox, (height, width), mode='bilinear')
    if width < max_pad:
        bbox = torch.nn.functional.pad(bbox, (0,max_pad-width, 0, 0), mode='constant')
        return bbox
    else:
        return None

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CRAFT reimplementation')


    parser.add_argument('--resume', default=None, type=str,
                        help='Checkpoint state_dict file to resume training from')
    parser.add_argument('--batch_size', default=128, type = int,
                        help='batch size of training')
    #parser.add_argument('--cdua', default=True, type=str2bool,
                        #help='Use CUDA to train model')
    parser.add_argument('--lr', '--learning-rate', default=3.2768e-5, type=float,
                        help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='Momentum value for optim')
    parser.add_argument('--weight_decay', default=5e-4, type=float,
                        help='Weight decay for SGD')
    parser.add_argument('--gamma', default=0.1, type=float,
                        help='Gamma update for SGD')
    parser.add_argument('--num_workers', default=32, type=int,
                        help='Number of workers used in dataloading')

    parser.add_argument('--config', type=str, default='cfgs/synth_exp001.yaml')
    parser.add_argument('--trained_model', default='./craft_mlt_25k.pth', type=str, help='pretrained model')
    parser.add_argument('--text_threshold', default=0.7, type=float, help='text confidence threshold')
    parser.add_argument('--low_text', default=0.4, type=float, help='text low-bound score')
    parser.add_argument('--link_threshold', default=0.4, type=float, help='link confidence threshold')
    parser.add_argument('--cuda', default=True, type=str2bool, help='Use cuda to train model')
    parser.add_argument('--canvas_size', default=2240, type=int, help='image size for inference')
    parser.add_argument('--mag_ratio', default=2, type=float, help='image magnification ratio')
    parser.add_argument('--poly', default=False, action='store_true', help='enable polygon type')
    parser.add_argument('--show_time', default=False, action='store_true', help='show processing time')
    parser.add_argument('--test_folder', default='/data/', type=str, help='folder path to input images')


    args = parser.parse_args()


    with open(args.config) as f:
        config = yaml.load(f)
    for k, v in config['common'].items():
        setattr(args, k, v)
    mkdirs(osp.join("logs/" + args.exp_name))
    mkdirs(osp.join("checkpoint", args.exp_name))
    mkdirs(osp.join("checkpoint", args.exp_name, "result"))

    logger = create_logger('global_logger', "logs/" + args.exp_name + '/log.txt')
    logger.info('{}'.format(args))

    for key, val in vars(args).items():
        logger.info("{:16} {}".format(key, val))



    # gaussian = gaussion_transform()
    # box = scio.loadmat('/data/CRAFT-pytorch/syntext/SynthText/gt.mat')
    # bbox = box['wordBB'][0][0][0]
    # charbox = box['charBB'][0]
    # imgname = box['imnames'][0]
    # imgtxt = box['txt'][0]

    #dataloader = syndata(imgname, charbox, imgtxt)
    dataloader = Synth80k('./data/SynthText', target_size = args.target_size, with_word=True)
    train_loader = torch.utils.data.DataLoader(
        dataloader,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=4,
        collate_fn = my_collate,
        drop_last=True,
        pin_memory=True)
    #batch_syn = iter(train_loader)
    # prefetcher = data_prefetcher(dataloader)
    # input, target1, target2 = prefetcher.next()
    #print(input.size())
    net = CRAFT()
    #net.load_state_dict(copyStateDict(torch.load('/data/CRAFT-pytorch/CRAFT_net_050000.pth')))
    #net.load_state_dict(copyStateDict(torch.load('/data/CRAFT-pytorch/1-7.pth')))
    #net.load_state_dict(copyStateDict(torch.load('/data/CRAFT-pytorch/craft_mlt_25k.pth')))
    #net.load_state_dict(copyStateDict(torch.load('vgg16_bn-6c64b313.pth')))
    #realdata = realdata(net)
    # realdata = ICDAR2015(net, '/data/CRAFT-pytorch/icdar2015', target_size = 768)
    # real_data_loader = torch.utils.data.DataLoader(
    #     realdata,
    #     batch_size=10,
    #     shuffle=True,
    #     num_workers=0,
    #     drop_last=True,
    #     pin_memory=True)
    net = net.cuda()
    #net = CRAFT_net

    # if args.cdua:
    net = torch.nn.DataParallel(net,range(torch.cuda.device_count())).cuda()
    cudnn.benchmark = True
    # realdata = ICDAR2015(net, '/data/CRAFT-pytorch/icdar2015', target_size=768)
    # real_data_loader = torch.utils.data.DataLoader(
    #     realdata,
    #     batch_size=10,
    #     shuffle=True,
    #     num_workers=0,
    #     drop_last=True,
    #     pin_memory=True)

    reco_model, converter, reco_criterion, reco_optimizer = generate_recognition_model(args)
    reco_model = torch.nn.DataParallel(reco_model, range(torch.cuda.device_count())).cuda()
    param = [
        {
        'params': net.parameters()},
        {
            'params': reco_model.parameters(), 'lr': args.reco_lr}
    ]
    optimizer = optim.SGD(param, lr=args.lr, weight_decay=args.weight_decay)
    criterion = Maploss()
    #criterion = torch.nn.MSELoss(reduce=True, size_average=True)
    net.train()

    step_index = 0


    loss_time = 0
    loss_value = 0
    compare_loss = 1

    batch_time = AverageMeter(100)
    iter_time = AverageMeter(100)

    loss_value = AverageMeter(10)
    reco_loss_value = AverageMeter(10)
    args.max_iters = args.num_epoch * len(train_loader)

    for epoch in range(args.num_epoch):
        # if epoch % 50 == 0 and epoch != 0:
        #     step_index += 1
        #     adjust_learning_rate(optimizer, args.gamma, step_index)

        for index, batches in enumerate(train_loader):

            st = time.time()
            images, gh_label, gah_label, mask,_,  word_region_torch = batches['torch_data']
            words = batches['list_data']

            '''
            bbox = (word_region_torch[0] == 1).nonzero().numpy()

            batch_size = word_region_torch.size(0)
            word_images = []
            word_labels = []
            for batch_index in range(batch_size):
                for word_index in range(len(words[batch_index])):
                    bbox = (word_region_torch[batch_index] == word_index+1).nonzero().numpy()
                    if len(bbox) == 4:
                        region = get_region(images[batch_index].numpy().transpose(1, 2, 0), bbox)
                        region = resize_pad(region, (32, 200)) if region is not None else None
                    else:
                        region = None
                    if region is not None:
                        region = region.mean(2)[:,:,np.newaxis]
                        word_images.append(region.transpose(2, 0, 1)[np.newaxis, :,:,:])
                        word_labels.append(words[batch_index][word_index])

            if len(word_images) > 0:
                word_images = np.concatenate(word_images, axis=0)
                word_images = torch.from_numpy(word_images)
            '''

            if index % 10000 == 0 and index != 0:
                step_index += 1
                adjust_learning_rate(optimizer, args.gamma, step_index)
            #real_images, real_gh_label, real_gah_label, real_mask = next(batch_real)
            idx = index + epoch * int(len(train_loader) / args.batch_size)

            # syn_images, syn_gh_label, syn_gah_label, syn_mask = next(batch_syn)
            # images = torch.cat((syn_images,real_images), 0)
            # gh_label = torch.cat((syn_gh_label, real_gh_label), 0)
            # gah_label = torch.cat((syn_gah_label, real_gah_label), 0)
            # mask = torch.cat((syn_mask, real_mask), 0)

            #affinity_mask = torch.cat((syn_mask, real_affinity_mask), 0)


            images = Variable(images.type(torch.FloatTensor)).cuda()
            images = images.contiguous()
            gh_label = gh_label.type(torch.FloatTensor)
            gah_label = gah_label.type(torch.FloatTensor)
            gh_label = Variable(gh_label).cuda()
            gah_label = Variable(gah_label).cuda()
            mask = mask.type(torch.FloatTensor)
            mask = Variable(mask).cuda()
            batch_time.update(time.time() - st)
            # affinity_mask = affinity_mask.type(torch.FloatTensor)
            # affinity_mask = Variable(affinity_mask).cuda()

            out, _, rec_feature = net(images.contiguous())

            optimizer.zero_grad()

            out1 = out[:, :, :, 0].cuda()
            out2 = out[:, :, :, 1].cuda()
            loss = criterion(gh_label, gah_label, out1, out2, mask)



            bbox = (word_region_torch[0] == 1).nonzero().numpy()

            batch_size = word_region_torch.size(0)
            word_images = []
            word_labels = []
            for batch_index in range(batch_size):
                for word_index in range(len(words[batch_index])):
                    bbox = (word_region_torch[batch_index] == word_index+1).nonzero().numpy()
                    if len(bbox) == 4:
                        region = get_region_torch(rec_feature[batch_index], bbox)
                        region = resize_pad(region, height=16, max_pad=400)
                        #region = resize_pad(region, (32, 200)) if region is not None else None
                    else:
                        region = None
                    if region is not None:
                        #region = region.mean(2)[:,:,np.newaxis]
                        word_images.append(region)
                        word_labels.append(words[batch_index][word_index])


            if len(word_images)>0:
                word_images = torch.cat(word_images, dim=0).cuda()
                text, length = converter.encode(word_labels)
                preds = reco_model(word_images, text)
                target = text[:, 1:]
                cost = reco_criterion(preds.view(-1, preds.shape[-1]), target.contiguous().view(-1))
                sum_loss = cost
                reco_loss_value.update(sum_loss.item())

            loss_value.update(loss.item())
            loss += sum_loss
            loss.backward()
            torch.nn.utils.clip_grad_norm_(reco_model.parameters(), args.grad_clip)
            optimizer.step()
            iter_time.update(time.time() - st)


            remain_iter = args.max_iters - (idx + epoch * int(len(train_loader)/args.batch_size))
            remain_time = remain_iter * iter_time.avg
            t_m, t_s = divmod(remain_time, 60)
            t_h, t_m = divmod(t_m, 60)

            remain_time = '{:02d}:{:02d}:{:02d}'.format(int(t_h), int(t_m), int(t_s))
            if index % args.print_freq == 0:
                logger.info('Iter = [{0}/{1}]\t'
                                'data time = {batch_time.avg:.3f}\t'
                                'iter time = {iter_time.avg:.3f}\t'
                                'reco_loss = {reco_loss.avg:.3f}\t'
                                'loss = {loss.avg:.4f}\t'.format(
                                    idx, args.max_iters, batch_time=batch_time,
                                    reco_loss=reco_loss_value,
                                    iter_time=iter_time,
                                    loss=loss_value))

                logger.info("remain_time: {}".format(remain_time))




            # if loss < compare_loss:
            #     print('save the lower loss iter, loss:',loss)
            #     compare_loss = loss
            #     torch.save(net.module.state_dict(),
            #                '/data/CRAFT-pytorch/real_weights/lower_loss.pth'

            if index % args.eval_iter== 0 and index != 0:
                print('Saving state, index:', index)
                torch.save(net.module.state_dict(),
                           './checkpoint/{}/synweights_'.format(args.exp_name) + repr(index) + '.pth')
                test('./checkpoint/{}/synweights_'.format(args.exp_name) + repr(index) + '.pth', args=args,
                     result_folder='./checkpoint/{}/result/'.format(args.exp_name))
                #test('/data/CRAFT-pytorch/craft_mlt_25k.pth')
                res_dict = getresult('./checkpoint/{}/result/'.format(args.exp_name))
                logger.info(res_dict['method'])








