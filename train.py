from data import *
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from ssd import build_ssd
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
from args import prepare_args
import mmdet.ops.dcn as dcn

args = prepare_args(VOC_ROOT)
if args.visdom:
    import visdom
    viz = visdom.Visdom()

torch.set_default_tensor_type('torch.cuda.FloatTensor')




def train():
    if args.dataset == 'COCO':
        if args.dataset_root == VOC_ROOT:
            if not os.path.exists(COCO_ROOT):
                parser.error('Must specify dataset_root if specifying dataset')
            print("WARNING: Using default COCO dataset_root because " +
                  "--dataset_root was not specified.")
            args.dataset_root = COCO_ROOT
        cfg = coco
        dataset = COCODetection(root=args.dataset_root,
                                transform=SSDAugmentation(cfg['min_dim'],
                                                          MEANS))
    elif args.dataset == 'VOC':
        #if args.dataset_root == COCO_ROOT:
            #parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc
        dataset = VOCDetection(root=args.dataset_root,
                               transform=SSDAugmentation(cfg['min_dim'],
                                                         MEANS))
        testset = VOCDetection(args.voc_root, [('2007', "test")],
                               BaseTransform(args.img_size, (104, 117, 123)),
                               VOCAnnotationTransform())



    ssd_net = build_ssd(args, 'train', cfg['min_dim'], cfg['num_classes'])
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)
        cudnn.benchmark = True

    if args.resume:
        model_name = "%s_%s_%s.pth"%(args.basenet, args.img_size, args.ft_iter)
        print('Resuming training from %s...'%(model_name))
        weights = torch.load(os.path.join(args.save_folder, model_name))
        ssd_net.load_state_dict(weights)
    else:
        vgg_weights = torch.load(os.path.join(args.save_folder, args.basenet))
        print('Loading base network...')
        ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.extras.apply(weights_init)
        if args.implementation in ["header", "190709"]:
            ssd_net.header.apply(weights_init)
        elif args.implementation == "vanilla":
            ssd_net.loc.apply(weights_init)
            ssd_net.conf.apply(weights_init)

    if args.optimizer.lower() == "adam":
        optimizer = optim.Adam(net.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    elif args.optimizer.lower() == "sgd":
        optimizer = optim.SGD(net.parameters(), lr=args.lr,  momentum=args.momentum,
                               weight_decay=args.weight_decay)
    criterion = MultiBoxLoss(cfg['num_classes'], args.overlap_threshold, True, 0,
                             True, 3, 0.5, False, args.cuda, rematch=args.rematch)

    net.train()
    # loss counters
    loc_loss = 0
    conf_loss = 0
    epoch = 0
    print('Loading the dataset...')

    epoch_size = len(dataset) // args.batch_size
    print('Training SSD on:', dataset.name)
    print('Using the specified args:')
    print(args)

    step_index = 0

    if args.visdom:
        vis_title = 'SSD.PyTorch on ' + dataset.name
        vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']
        iter_plot = create_vis_plot('Iteration', 'Loss', vis_title, vis_legend)
        epoch_plot = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)

    data_loader = data.DataLoader(dataset, args.batch_size,
                                  num_workers=args.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)
    # create batch iterator
    batch_iterator = iter(data_loader)
    loc_losses, conf_losses = [], []
    progress = open(os.path.join(args.save_folder, "%s_train_prog.txt" % args.name), "w")
    for iteration in range(args.start_iter, args.max_iter):
        #print("iteration: %s"%iteration)
        if args.visdom and iteration != 0 and (iteration % epoch_size == 0):
            update_vis_plot(epoch, loc_loss, conf_loss, epoch_plot, None,
                            'append', epoch_size)
            # reset epoch loss counters
            loc_loss = 0
            conf_loss = 0
            epoch += 1

        if iteration in cfg['lr_steps']:
            step_index += 1
            adjust_learning_rate(optimizer, args.gamma, step_index)

        # load train data
        try:
            images, targets = next(batch_iterator)
        except StopIteration:
            batch_iterator = iter(data_loader)
            images, targets = next(batch_iterator)
        t0 = time.time()
        images = images.cuda()
        targets = [ann.cuda() for ann in targets]

        #targets_idx = [ann.size(0) for ann in targets]
        #targets_idx = torch.cuda.LongTensor([sum(targets_idx[:_idx]) for _idx in range(len(targets_idx))]).unsqueeze(-1)
        #targets = torch.cat(targets, dim=0).cuda()
        # forward
        out = net(images)#, targets, targets_idx)
        # backprop
        optimizer.zero_grad()
        loss_l, loss_c = criterion(out, targets)
        loss = loss_l + loss_c
        loss.backward()
        optimizer.step()
        loc_loss += loss_l.data
        conf_loss += loss_c.data
        loc_losses.append(float(loss_l.data))
        conf_losses.append(float(loss_c.data))
        result = "--loc_loss: %.4f conf_loss: %.4f--\n"%(float(loss_l.data), float(loss_c.data))
        progress.write(result)
        if iteration > 0 and iteration % 10 == 0:
            t1 = time.time()
            print('timer: %.4f sec.' % (t1 - t0))
            print('iter ' + repr(iteration) + ' || Loss: %.4f || Conf_Loss: %.4f || Loc_Loss: %.4f ||' % (loss.data, loss_c.data, loss_l.data), end=' ')

        if args.visdom:
            update_vis_plot(iteration, loss_l.data, loss_c.data,
                            iter_plot, epoch_plot, 'append')

        if iteration > 5000 and iteration % 2000 == 0:
            print('Saving state, iter:', iteration)
            torch.save(ssd_net.state_dict(), os.path.join(args.save_folder,
                                                          '%s_%s_%s.pth'%(args.name, args.img_size, repr(iteration))))
    progress.close()


def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr



def weights_init(m):
    if type(m) == torch.nn.Linear:
        torch.nn.init.xavier_normal_(m.weight)
    elif type(m) == torch.nn.Conv2d:
        torch.nn.init.kaiming_normal_(m.weight)
    elif isinstance(m, torch.nn.BatchNorm2d):
        torch.nn.init.constant_(m.weight, 1)
        torch.nn.init.constant_(m.bias, 0)
    else:
        if type(m) is nn.ModuleList:
            for _m in m:
                if type(_m) == torch.nn.Linear:
                    torch.nn.init.xavier_normal_(_m.weight)
                elif type(_m) == torch.nn.Conv2d:
                    torch.nn.init.kaiming_normal_(_m.weight)
                elif isinstance(_m, torch.nn.BatchNorm2d):
                    torch.nn.init.constant_(_m.weight, 1)
                    torch.nn.init.constant_(_m.bias, 0)
        elif type(m) is dcn.DeformConv:
            torch.nn.init.kaiming_normal_(m.weight)
        else:
            pass


def create_vis_plot(_xlabel, _ylabel, _title, _legend):
    return viz.line(
        X=torch.zeros((1,)).cpu(),
        Y=torch.zeros((1, 3)).cpu(),
        opts=dict(
            xlabel=_xlabel,
            ylabel=_ylabel,
            title=_title,
            legend=_legend
        )
    )


def update_vis_plot(iteration, loc, conf, window1, window2, update_type,
                    epoch_size=1):
    viz.line(
        X=torch.ones((1, 3)).cpu() * iteration,
        Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu() / epoch_size,
        win=window1,
        update=update_type
    )
    # initialize epoch plot on first iteration
    if iteration == 0:
        viz.line(
            X=torch.zeros((1, 3)).cpu(),
            Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
            win=window2,
            update=True
        )


if __name__ == '__main__':
    train()
