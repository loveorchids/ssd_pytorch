# -*- coding: utf-8 -*-
import torch


def point_form(boxes):
    """ Convert prior_boxes to (xmin, ymin, xmax, ymax)
    representation for comparison to point form ground truth data.
    Args:
        boxes: (tensor) center-size default boxes from priorbox layers.
    Return:
        boxes: (tensor) Converted xmin, ymin, xmax, ymax form of boxes.
    """
    return torch.cat((boxes[:, :2] - boxes[:, 2:]/2,     # xmin, ymin
                     boxes[:, :2] + boxes[:, 2:]/2), 1)  # xmax, ymax


def center_size(boxes):
    """ Convert prior_boxes to (cx, cy, w, h)
    representation for comparison to center-size form ground truth data.
    Args:
        boxes: (tensor) point_form boxes
    Return:
        boxes: (tensor) Converted xmin, ymin, xmax, ymax form of boxes.
    """
    return torch.cat([(boxes[:, 2:] + boxes[:, :2])/2,  # cx, cy
                     boxes[:, 2:] - boxes[:, :2]], 1)  # w, h


def intersect(box_a, box_b):
    """ We resize both tensors to [A,B,2] without new malloc:
    [A,2] -> [A,1,2] -> [A,B,2]
    [B,2] -> [1,B,2] -> [A,B,2]
    Then we compute the area of intersect between box_a and box_b.
    Args:
      box_a: (tensor) bounding boxes, Shape: [A,4].
      box_b: (tensor) bounding boxes, Shape: [B,4].
    Return:
      (tensor) intersection area, Shape: [A,B].
    """
    A = box_a.size(0)
    B = box_b.size(0)
    max_xy = torch.min(box_a[:, 2:].unsqueeze(1).expand(A, B, 2),
                       box_b[:, 2:].unsqueeze(0).expand(A, B, 2))
    min_xy = torch.max(box_a[:, :2].unsqueeze(1).expand(A, B, 2),
                       box_b[:, :2].unsqueeze(0).expand(A, B, 2))
    inter = torch.clamp((max_xy - min_xy), min=0)
    return inter[:, :, 0] * inter[:, :, 1]


def jaccard(box_a, box_b):
    """Compute the jaccard overlap of two sets of boxes.  The jaccard overlap
    is simply the intersection over union of two boxes.  Here we operate on
    ground truth boxes and default boxes.
    E.g.:
        A ∩ B / A ∪ B = A ∩ B / (area(A) + area(B) - A ∩ B)
    Args:
        box_a: (tensor) Ground truth bounding boxes, Shape: [num_objects,4]
        box_b: (tensor) Prior boxes from priorbox layers, Shape: [num_priors,4]
    Return:
        jaccard overlap: (tensor) Shape: [box_a.size(0), box_b.size(0)]
    """
    inter = intersect(box_a, box_b)
    area_a = ((box_a[:, 2]-box_a[:, 0]) *
              (box_a[:, 3]-box_a[:, 1])).unsqueeze(1).expand_as(inter)  # [A,B]
    area_b = ((box_b[:, 2]-box_b[:, 0]) *
              (box_b[:, 3]-box_b[:, 1])).unsqueeze(0).expand_as(inter)  # [A,B]
    union = area_a + area_b - inter
    return inter / union  # [A,B]


def match(threshold, truths, priors, variances, labels, loc_t, conf_t, idx, visualize=False):
    """Match each prior box with the ground truth box of the highest jaccard
    overlap, encode the bounding boxes, then return the matched indices
    corresponding to both confidence and location preds.
    Args:
        threshold: (float) The overlap threshold used when mathing boxes.
        truths: (tensor) Ground truth boxes, Shape: [num_obj, num_priors].
        priors: (tensor) Prior boxes from priorbox layers, Shape: [n_priors,4].
        variances: (tensor) Variances corresponding to each prior coord,
            Shape: [num_priors, 4].
        labels: (tensor) All the class labels for the image, Shape: [num_obj].
        loc_t: (tensor) Tensor to be filled w/ endcoded location targets.
        conf_t: (tensor) Tensor to be filled w/ matched indices for conf preds.
        idx: (int) current batch index
    Return:
        The matched indices corresponding to 1)location and 2)confidence preds.
    """
    # jaccard index
    overlaps = jaccard(
        truths,
        point_form(priors)
    )
    # (Bipartite Matching)
    # [1,num_objects] best prior for each ground truth
    best_prior_overlap, best_prior_idx = overlaps.max(1, keepdim=True)
    # [1,num_priors] best ground truth for each prior
    best_truth_overlap, best_truth_idx = overlaps.max(0, keepdim=True)

    #tmp = best_truth_overlap.repeat(truths.size(0), 1) - overlaps
    #(torch.sum(tmp > 0.2, dim=0) == 3) * (best_truth_overlap.squeeze() > threshold)

    best_truth_idx.squeeze_(0)
    best_truth_overlap.squeeze_(0)
    best_prior_idx.squeeze_(1)
    best_prior_overlap.squeeze_(1)
    best_truth_idx[best_prior_idx] = torch.arange(best_prior_idx.size(0))
    #print(best_truth_idx[best_prior_idx])
    #best_truth_overlap.index_fill_(0, best_prior_idx, 2)  # ensure best prior
    # ensure every gt matches with its prior of max overlap
    for j in range(best_prior_idx.size(0)):
        best_truth_idx[best_prior_idx[j]] = j
    #print(best_truth_idx[best_prior_idx])
    #print("")
    matches = truths[best_truth_idx]          # Shape: [num_priors,4]
    conf = labels[best_truth_idx] + 1         # Shape: [num_priors]
    conf[best_truth_overlap < threshold] = 0  # label as background
    if visualize:
        return overlaps, conf
    loc = encode(matches, priors, variances)
    loc_t[idx] = loc    # [num_priors,4] encoded offsets to learn
    conf_t[idx] = conf  # [num_priors] top class label for each prior


def encode(matched, priors, variances):
    """Encode the variances from the priorbox layers into the ground truth boxes
    we have matched (based on jaccard overlap) with the prior boxes.
    Args:
        matched: (tensor) Coords of ground truth for each prior in point-form
            Shape: [num_priors, 4].
        priors: (tensor) Prior boxes in center-offset form
            Shape: [num_priors,4].
        variances: (list[float]) Variances of priorboxes
    Return:
        encoded boxes (tensor), Shape: [num_priors, 4]
    """

    # dist b/t match center and prior's center
    g_cxcy = (matched[:, :2] + matched[:, 2:])/2 - priors[:, :2]
    # encode variance
    g_cxcy /= (variances[0] * priors[:, 2:])
    # match wh / prior wh
    g_wh = (matched[:, 2:] - matched[:, :2]) / priors[:, 2:]
    g_wh = torch.log(g_wh) / variances[1]
    # return target for smooth_l1_loss
    return torch.cat([g_cxcy, g_wh], 1)  # [num_priors,4]


# Adapted from https://github.com/Hakuyume/chainer-ssd
def decode(loc, priors, variances):
    """Decode locations from predictions using priors to undo
    the encoding we did for offset regression at train time.
    Args:
        loc (tensor): location predictions for loc layers,
            Shape: [num_priors,4]
        priors (tensor): Prior boxes in center-offset form.
            Shape: [num_priors,4].
        variances: (list[float]) Variances of priorboxes
    Return:
        decoded bounding box predictions
    """

    boxes = torch.cat((
        priors[:, :2] + loc[:, :2] * variances[0] * priors[:, 2:],
        priors[:, 2:] * torch.exp(loc[:, 2:] * variances[1])), 1)
    boxes[:, :2] -= boxes[:, 2:] / 2
    boxes[:, 2:] += boxes[:, :2]
    return boxes


def log_sum_exp(x):
    """Utility function for computing log_sum_exp while determining
    This will be used to determine unaveraged confidence loss across
    all examples in a batch.
    Args:
        x (Variable(tensor)): conf_preds from conf layers
    """
    x_max = x.data.max()
    return torch.log(torch.sum(torch.exp(x-x_max), 1, keepdim=True)) + x_max


# Original author: Francisco Massa:
# https://github.com/fmassa/object-detection.torch
# Ported to PyTorch by Max deGroot (02/01/2017)
def nms(boxes, scores, overlap=0.5, top_k=200):
    """Apply non-maximum suppression at test time to avoid detecting too many
    overlapping bounding boxes for a given object.
    Args:
        boxes: (tensor) The location preds for the img, Shape: [num_priors,4].
        scores: (tensor) The class predscores for the img, Shape:[num_priors].
        overlap: (float) The overlap thresh for suppressing unnecessary boxes.
        top_k: (int) The Maximum number of box preds to consider.
    Return:
        The indices of the kept boxes with respect to num_priors.
    """

    keep = scores.new(scores.size(0)).zero_().long()
    if boxes.numel() == 0:
        return keep
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    area = torch.mul(x2 - x1, y2 - y1)
    v, idx = scores.sort(0)  # sort in ascending order
    # I = I[v >= 0.01]
    idx = idx[-top_k:]  # indices of the top-k largest vals
    xx1 = boxes.new()
    yy1 = boxes.new()
    xx2 = boxes.new()
    yy2 = boxes.new()
    w = boxes.new()
    h = boxes.new()

    # keep = torch.Tensor()
    count = 0
    while idx.numel() > 0:
        i = idx[-1]  # index of current largest val
        # keep.append(i)
        keep[count] = i
        count += 1
        if idx.size(0) == 1:
            break
        idx = idx[:-1]  # remove kept element from view
        # load bboxes of next highest vals
        torch.index_select(x1, 0, idx, out=xx1)
        torch.index_select(y1, 0, idx, out=yy1)
        torch.index_select(x2, 0, idx, out=xx2)
        torch.index_select(y2, 0, idx, out=yy2)
        # store element-wise max with next highest score
        xx1 = torch.clamp(xx1, min=x1[i])
        yy1 = torch.clamp(yy1, min=y1[i])
        xx2 = torch.clamp(xx2, max=x2[i])
        yy2 = torch.clamp(yy2, max=y2[i])
        w.resize_as_(xx2)
        h.resize_as_(yy2)
        w = xx2 - xx1
        h = yy2 - yy1
        # check sizes of xx1 and xx2.. after each iteration
        w = torch.clamp(w, min=0.0)
        h = torch.clamp(h, min=0.0)
        inter = w*h
        # IoU = i / (area(a) + area(b) - i)
        rem_areas = torch.index_select(area, 0, idx)  # load remaining areas)
        union = (rem_areas - inter) + area[i]
        IoU = inter/union  # store result in iou
        # keep only elements with an IoU <= overlap
        idx = idx[IoU.le(overlap)]
    return keep, count

def add_noise(bboxes, kernel_size, v3_form):
    ratios = (bboxes[:, 2] - bboxes[:, 0]) / (bboxes[:, 3] - bboxes[:, 1])
    max_length = torch.max(torch.stack(((bboxes[:, 2] - bboxes[:, 0]), (bboxes[:, 3] - bboxes[:, 1])), dim=1), dim=1)[0]
    # ratios计算方法为宽高比，所以small_idx代表比较高的box
    small_idx = ratios < 1
    one_idx = (ratios >= 0.9) * (ratios <= 1.1)
    # 将ratios中所有小于1的index取倒数
    ratios[small_idx] = 1 / ratios[small_idx]
    # offsets = ratios / (kernel_size ** 2)
    # offsets = (torch.tanh(0.3 * (ratios - 5)) / 2 + 0.5) * max_length / (kernel_size ** 2)
    offsets = torch.tanh(0.25 * ratios) * max_length / (kernel_size ** 2)
    offsets[one_idx] = 0
    offsets = offsets.unsqueeze(-1).repeat(1, 2 * kernel_size ** 2)

    assert kernel_size == 3, "偏移量是为kernel size=3时设计的"
    if v3_form:
        distortion = torch.FloatTensor([0, -1, 0, -1, 0, -1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1]).cuda(
            bboxes.device.index)
    else:
        distortion = torch.FloatTensor([-1, 0, -1, 0, -1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1, 0]).cuda(
            bboxes.device.index)
    distortion2 = distortion.view(kernel_size, kernel_size, 2).permute(1, 0, 2)[:, :, (1, 0)].contiguous().view(-1)
    distortions = distortion.unsqueeze(0).repeat(bboxes.size(0), 1)
    distortions[small_idx] = distortion2
    distortions = distortions * offsets
    noise = torch.randn(distortions.size(0)) < 0
    # 产生随机偏移方向。如果box较高，左侧的centroid会向上也会向下偏移（右侧与左侧相反）
    # 如果box较宽，上方的点会向左或向右偏移（上方与下方偏移方向相反）
    distortions[noise] = distortions[noise] * -1
    return distortions

def center_conv_point(bboxes, kernel_size=3, c_min=0, c_max=1, v3_form=False):
    """In a parallel manner also keeps the gradient during BP"""
    #bboxes.clamp_(min=c_min, max=c_max)
    if v3_form:
        base = torch.cat([bboxes[:, :2][:, (1, 0)]] * (kernel_size ** 2), dim=1)
    else:
        base = torch.cat([bboxes[:, :2]] * (kernel_size ** 2), dim=1)
    multiplier = torch.tensor([(2 * i + 1) / kernel_size / 2
                               for i in range(kernel_size)]).cuda(bboxes.device.index)
    # multiplier生成的时候顺序先从上往下数，再从左往右数
    # 应当换成先从左往右数，再从上往下数的顺序，所以有了[:, :, (1, 0)]
    multiplier = torch.stack(torch.meshgrid([multiplier, multiplier]),
                             dim=-1).contiguous().view(-1)
    multiplier = multiplier.unsqueeze(0).repeat(bboxes.size(0), 1)
    if v3_form:
        center = torch.stack([bboxes[:, 3] - bboxes[:, 1], bboxes[:, 2] - bboxes[:, 0]], dim=-1)
    else:
        center = torch.stack([bboxes[:, 2] - bboxes[:, 0], bboxes[:, 3] - bboxes[:, 1]], dim=-1)
    center = torch.cat([center] * (kernel_size ** 2), dim=1)
    return base + center * multiplier# + distortions


def measure(pred_boxes, gt_boxes, width, height):
    if gt_boxes.size(0) == 0 and pred_boxes.size(0) == 0:
        return 1.0, 1.0, 1.0
    elif gt_boxes.size(0) == 0 and pred_boxes.size(0) != 0:
        return 0.0, 0.0, 0.0
    elif gt_boxes.size(0) != 0 and pred_boxes.size(0) == 0:
        return 0.0, 0.0, 0.0
    else:
        """
        scale = torch.Tensor([width, height, width, height])
        canvas_p, canvas_g = [torch.zeros(height, width).byte()] * 2
        if pred_boxes.is_cuda or gt_boxes.is_cuda:
            scale = scale.cuda()
            canvas_p = canvas_p.cuda()
            canvas_g = canvas_g.cuda()
        max_size = max(width, height)
        scaled_p = (scale.unsqueeze(0).expand_as(pred_boxes) * pred_boxes).long().clamp_(0, max_size)
        scaled_g = (scale.unsqueeze(0).expand_as(gt_boxes) * gt_boxes).long().clamp_(0, max_size)
        for g in scaled_g:
            canvas_g[g[0]:g[2], g[1]:g[3]] = 1
        for p in scaled_p:
            canvas_p[p[0]:p[2], p[1]:p[3]] = 1
        inter = canvas_g * canvas_p
        union = canvas_g + canvas_p >= 1

        vb.plot_tensor(args, inter.permute(1, 0).unsqueeze(0).unsqueeze(0), margin=0, path=PIC+"tmp_inter.jpg")
        vb.plot_tensor(args, union.permute(1, 0).unsqueeze(0).unsqueeze(0), margin=0, path=PIC+"tmp_union.jpg")
        vb.plot_tensor(args, canvas_g.permute(1, 0).unsqueeze(0).unsqueeze(0), margin=0, path=PIC+"tmp_gt.jpg")
        vb.plot_tensor(args, canvas_p.permute(1, 0).unsqueeze(0).unsqueeze(0), margin=0, path=PIC+"tmp_pd.jpg")
        """
        inter = intersect(pred_boxes, gt_boxes)
        text_area = get_box_size(pred_boxes)
        gt_area = get_box_size(gt_boxes)
        num_sample = max(text_area.size(0),  gt_area.size(0))
        accuracy = torch.sum(jaccard(pred_boxes, gt_boxes).max(0)[0]) / num_sample
        precision = torch.sum(inter.max(1)[0] / text_area) / num_sample
        recall = torch.sum(inter.max(0)[0] / gt_area) / num_sample
        return float(accuracy), float(precision), float(recall)

def get_box_size(box):
    """
    calculate the bound box size
    """
    return (box[:, 2]-box[:, 0]) * (box[:, 3]-box[:, 1])

def coord_to_rect(coord, height, width):
    """
    Convert 4 point boundbox coordinate to matplotlib rectangle coordinate
    """
    x1, y1, x2, y2 = coord[0], coord[1], coord[2] - coord[0], coord[3] - coord[1]
    return int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)


def get_parameter(param):
    """
    Convert input parameter to two parameter if they are lists or tuples
    Mainly used in tb_vis.py and tb_model.py
    """
    if type(param) is list or type(param) is tuple:
        assert len(param) == 2, "input parameter shoud be either scalar or 2d list or tuple"
        p1, p2 = param[0], param[1]
    else:
        p1, p2 = param, param
    return p1, p2

def calculate_anchor_number(cfg, i):
    return 2 + 2* len(cfg['aspect_ratios'][i])