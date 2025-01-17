import torch.nn as nn
import torch, os
from torchvision import transforms
import numpy as np
import cv2
from torch.autograd import Variable

from functools import partial
from torch import nn, Tensor
from torch.nn import functional as F
from typing import Any, Callable, Dict, List, Optional, Sequence
from torch.utils.data import Dataset, DataLoader


class LPRAtt(nn.Module):
    def __init__(self, lpr_max_len, phase, class_num, dropout_rate):
        super(LPRAtt, self).__init__()
        self.phase = phase
        self.lpr_max_len = lpr_max_len
        self.class_num = class_num
        self.backbone = nn.Sequential(
            nn.Conv2d(in_channels=3, out_channels=64, kernel_size=3, stride=1), # 0
            nn.BatchNorm2d(num_features=64),
            nn.ReLU(),  # 2
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 1, 1)),
           
            Conv_CBAM(ch_in=64, ch_out=128),    # *** 4 ***

            nn.BatchNorm2d(num_features=128),
            nn.ReLU(),  # 6
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(2, 1, 2)),

            Conv_CBAM(ch_in=64, ch_out=256),   # 8

            nn.BatchNorm2d(num_features=256),
            nn.ReLU(),  # 10

            Conv_CBAM(ch_in=256, ch_out=256),   # *** 11 ***

            nn.BatchNorm2d(num_features=256),   # 12
            nn.ReLU(),

            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(4, 1, 2)),  # 14
            nn.Dropout(dropout_rate),

            nn.Conv2d(in_channels=64, out_channels=256, kernel_size=(1, 4), stride=1),  # 16
            nn.BatchNorm2d(num_features=256),
            nn.ReLU(),  # 18
            nn.Dropout(dropout_rate),

            nn.Conv2d(in_channels=256, out_channels=class_num, kernel_size=(13, 1), stride=1), # 20
            nn.BatchNorm2d(num_features=class_num),
            nn.ReLU(),  # *** 22 ***
        )
        self.container = nn.Sequential(
            nn.Conv2d(in_channels=448+self.class_num, out_channels=self.class_num, kernel_size=(1, 1), stride=(1, 1)),
        )

    def forward(self, x):
        keep_features = list()
        for i, layer in enumerate(self.backbone.children()):
            x = layer(x)
            if i in [2, 6, 13, 22]: # [2, 4, 8, 11, 22]
                keep_features.append(x)

        global_context = list()
        for i, f in enumerate(keep_features):
            if i in [0, 1]:
                f = nn.AvgPool2d(kernel_size=5, stride=5)(f)
            if i in [2]:
                f = nn.AvgPool2d(kernel_size=(4, 10), stride=(4, 2))(f)
            f_pow = torch.pow(f, 2)
            f_mean = torch.mean(f_pow)
            f = torch.div(f, f_mean)
            global_context.append(f)

        x = torch.cat(global_context, 1)
        x = self.container(x)
        logits = torch.mean(x, dim=2)

        return logits

def build_LPRAtt(lpr_max_len=8, phase=False, class_num=66, dropout_rate=0.5):

    Net = LPRAtt(lpr_max_len, phase, class_num, dropout_rate)

    if phase == "train":
        return Net.train()
    else:
        return Net.eval()

def autopad(k, p=None):  # kernel, padding
    # Pad to 'same'
    if p is None:
        p = k // 2 if isinstance(k, int) else [x // 2 for x in k]  # auto-pad
    return p

# 标准卷积层 + CBAM
class Conv_CBAM(nn.Module):
    # Standard convolution
    def __init__(self, ch_in, ch_out, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Conv_CBAM, self).__init__()
        self.conv = nn.Conv2d(ch_in, ch_out, k, s, autopad(k, p), groups=g, bias=False)
        self.bn = nn.BatchNorm2d(ch_out)
        self.act = nn.Hardswish() if act else nn.Identity()
        self.ca = ChannelAttention(ch_out)
        self.sa = SpatialAttention()

    def forward(self, x):
        x = self.act(self.bn(self.conv(x)))
        x = self.ca(x) * x
        x = self.sa(x) * x
        return x

    def fuseforward(self, x):
        return self.act(self.conv(x))

# add CBAM
class ChannelAttention(nn.Module):
    def __init__(self, in_planes, ratio=16):
        super(ChannelAttention, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)

        self.fc1   = nn.Conv2d(in_planes, in_planes // 16, 1, bias=False)
        self.relu1 = nn.ReLU()
        self.fc2   = nn.Conv2d(in_planes // 16, in_planes, 1, bias=False)

        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avg_out = self.fc2(self.relu1(self.fc1(self.avg_pool(x))))
        max_out = self.fc2(self.relu1(self.fc1(self.max_pool(x))))
        out = avg_out + max_out
        return self.sigmoid(out)


class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()

        assert kernel_size in (3, 7), 'kernel size must be 3 or 7'
        padding = 3 if kernel_size == 7 else 1

        self.conv1 = nn.Conv2d(2, 1, kernel_size, padding=padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avg_out = torch.mean(x, dim=1, keepdim=True)
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        x = torch.cat([avg_out, max_out], dim=1)
        x = self.conv1(x)
        return self.sigmoid(x)


CHARS = ['京', '沪', '津', '渝', '冀', '晋', '蒙', '辽', '吉', '黑',
         '苏', '浙', '皖', '闽', '赣', '鲁', '豫', '鄂', '湘', '粤',
         '桂', '琼', '川', '贵', '云', '藏', '陕', '甘', '青', '宁',
         '新',
         '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
         'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K',
         'L', 'M', 'N', 'P', 'Q', 'R', 'S', 'T', 'U', 'V',
         'W', 'X', 'Y', 'Z', 'I', 'O', '-'
         ]

CHARS_DICT = {char:i for i, char in enumerate(CHARS)}

def vec2text(prebs):
    # greedy decode
    prebs = prebs.cpu().detach().numpy()
    preb_labels = list()
    for i in range(prebs.shape[0]):
        preb = prebs[i, :, :]
        preb_label = list()
        for j in range(preb.shape[1]):
            preb_label.append(np.argmax(preb[:, j], axis=0))
        no_repeat_blank_label = list()
        pre_c = preb_label[0]
        if pre_c != len(CHARS) - 1:
            no_repeat_blank_label.append(pre_c)
        for c in preb_label: # dropout repeate label and blank label
            if (pre_c == c) or (c == len(CHARS) - 1):
                if c == len(CHARS) - 1:
                    pre_c = c
                continue
            no_repeat_blank_label.append(c)
            pre_c = c
        preb_labels.append(no_repeat_blank_label)
        
        lb = ""
        for i in no_repeat_blank_label:
            lb += CHARS[i]
    return lb

net = build_LPRAtt(lpr_max_len=8, phase=False, class_num=len(CHARS), dropout_rate=0.5)

net.cuda()

class MyDataset(Dataset):
    def __init__(self, crop_img, transform=None):
        super(Dataset, self).__init__()

        # self.transform = transforms.Compose([
        #     transforms.ToPILImage(),
        #     transforms.Resize([24, 94]),
        #     transforms.ToTensor(),    
        # ])
        self.crop_img = crop_img
        self.PreprocFun = self.transform_s
        
    def __len__(self):
        return 1

    def __getitem__(self, index):

        Image = cv2.resize(self.crop_img, [94,24])
        img = self.PreprocFun(Image)

        # if self.transform is not None:
            # img = self.transform(self.crop_img)

        return img

    def transform_s(self, img):
        # img = img.permute(1, 2, 0)
        img = img.astype('float32')
        img -= 127.5
        img *= 0.0078125
        img = np.transpose(img, (2, 0, 1))
        # img = img.permute(2, 0, 1)

        return img


def predict(crop_img):
    net.eval()
    crop_img = Variable(crop_img.cuda())
    with torch.no_grad():
        prebs = net(crop_img)
    return vec2text(prebs)

def get_label(model_path, crop_img):
    
    if os.path.exists(model_path):
        net.load_state_dict(torch.load(model_path))

    # transform = transforms.Compose([transforms.ToTensor()]) 
    vil_data = MyDataset(crop_img, transform=True)
    vil_data_loader = DataLoader(vil_data, batch_size=1, num_workers=4, shuffle=True)


    for img in vil_data_loader:
        pre = predict(img)

    return pre
