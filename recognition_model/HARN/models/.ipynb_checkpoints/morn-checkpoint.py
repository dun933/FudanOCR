import torch
import torch.nn as nn
from torch.autograd import Variable
import numpy as np


class MORN(nn.Module):
    def __init__(self, nc, targetH, targetW, inputDataType='torch.cuda.FloatTensor', maxBatch=256, CUDA=True, log=None):
        super(MORN, self).__init__()
        self.targetH = targetH
        self.targetW = targetW
        self.inputDataType = inputDataType
        self.maxBatch = maxBatch
        self.cuda = CUDA
        self.log = log

        self.cnn = nn.Sequential(
            nn.MaxPool2d(2, 2),
            nn.Conv2d(nc, 64, 3, 1, 1), nn.BatchNorm2d(64), nn.ReLU(True), nn.MaxPool2d(2, 2),
            nn.Conv2d(64, 128, 3, 1, 1), nn.BatchNorm2d(128), nn.ReLU(True), nn.MaxPool2d(2, 2),
            nn.Conv2d(128, 64, 3, 1, 1), nn.BatchNorm2d(64), nn.ReLU(True),
            nn.Conv2d(64, 16, 3, 1, 1), nn.BatchNorm2d(16), nn.ReLU(True),
            nn.Conv2d(16, 2, 3, 1, 1), nn.BatchNorm2d(2)
        )

        self.pool = nn.MaxPool2d(2, 1)

        h_list = np.arange(self.targetH) * 2. / (self.targetH - 1) - 1
        w_list = np.arange(self.targetW) * 2. / (self.targetW - 1) - 1

        grid = np.meshgrid(
            w_list,
            h_list,
            indexing='ij'
        )
        grid = np.stack(grid, axis=-1)
        grid = np.transpose(grid, (1, 0, 2))
        grid = np.expand_dims(grid, 0)
        grid = np.tile(grid, [maxBatch, 1, 1, 1])
        grid = torch.from_numpy(grid).type(self.inputDataType)
        if self.cuda:
            grid = grid.cuda()

        self.grid = Variable(grid, requires_grad=False)
        self.grid_x = self.grid[:, :, :, 0].unsqueeze(3)
        self.grid_y = self.grid[:, :, :, 1].unsqueeze(3)

    def forward(self, x, test, enhance=1, debug=False, steps=None):

        if not test and np.random.random() > 0.5:
            return nn.functional.interpolate(x, size=(self.targetH, self.targetW), mode='bilinear')
        if not test:
            enhance = 0

        assert x.size(0) <= self.maxBatch
        assert x.data.type() == self.inputDataType

        grid = self.grid[:x.size(0)]
        grid_x = self.grid_x[:x.size(0)]
        grid_y = self.grid_y[:x.size(0)]
        x_small = nn.functional.interpolate(x, size=(self.targetH, self.targetW), mode='bilinear')

        offsets = self.cnn(x_small)
        offsets_posi = nn.functional.relu(offsets, inplace=False)
        offsets_nega = nn.functional.relu(-offsets, inplace=False)
        offsets_pool = self.pool(offsets_posi) - self.pool(offsets_nega)

        offsets_grid = nn.functional.grid_sample(offsets_pool, grid)
        offsets_grid = offsets_grid.permute(0, 2, 3, 1).contiguous()
        offsets_grid_x = offsets_grid[:, :, :, 0].unsqueeze(3)
        offsets_grid_y = offsets_grid[:, :, :, 1].unsqueeze(3)
        offsets_x = torch.cat([grid_x + offsets_grid_x, grid_y + offsets_grid_y], 3)
        # offsets_x = torch.cat([grid_x, grid_y + offsets_grid], 3)
        x_rectified = nn.functional.grid_sample(x, offsets_x)

        for iteration in range(enhance):
            offsets = self.cnn(x_rectified)

            offsets_posi = nn.functional.relu(offsets, inplace=False)
            offsets_nega = nn.functional.relu(-offsets, inplace=False)
            offsets_pool = self.pool(offsets_posi) - self.pool(offsets_nega)

            offsets_grid += nn.functional.grid_sample(offsets_pool, grid).permute(0, 2, 3, 1).contiguous()
            offsets_grid_x = offsets_grid[:, :, :, 0].unsqueeze(3)
            offsets_grid_y = offsets_grid[:, :, :, 1].unsqueeze(3)
            offsets_x = torch.cat([grid_x + offsets_grid_x, grid_y + offsets_grid_y], 3)
            # offsets_x = torch.cat([grid_x, grid_y + offsets_grid], 3)
            x_rectified = nn.functional.grid_sample(x, offsets_x)

        if debug:

            offsets_mean = torch.mean(offsets_grid.view(x.size(0), -1), 1)
            offsets_max, _ = torch.max(offsets_grid.view(x.size(0), -1), 1)
            offsets_min, _ = torch.min(offsets_grid.view(x.size(0), -1), 1)

            import matplotlib.pyplot as plt
            from colour import Color
            from torchvision import transforms
            import cv2

            alpha = 0.7
            density_range = 256
            cmap = plt.get_cmap("rainbow")
            blue = Color("blue")
            hex_colors = list(blue.range_to(Color("red"), density_range))
            rgb_colors = [[rgb * 255 for rgb in color.rgb] for color in hex_colors][::-1]
            to_pil_image = transforms.ToPILImage()

            for i in range(1):

                img_small = x_small[i].data.cpu().mul_(0.5).add_(0.5)
                img = to_pil_image(img_small)
                img = np.array(img)
                if len(img.shape) == 2:
                    img = cv2.merge([img.copy()] * 3)
                img_copy_x = img.copy()
                img_copy_y = img.copy()

                v_max = offsets_max.data[i].cpu()
                v_min = offsets_min.data[i].cpu()
                img_offsets_x = (offsets_grid[i][:, :, 0]).view(1, self.targetH, self.targetW).data.cpu().add_(-v_min).mul_(
                    1. / (v_max - v_min))
                img_offsets_y = (offsets_grid[i][:, :, 1]).view(1, self.targetH, self.targetW).data.cpu().add_(-v_min).mul_(
                    1. / (v_max - v_min))
                img_offsets_x = to_pil_image(img_offsets_x)
                img_offsets_y = to_pil_image(img_offsets_y)
                img_offsets_x = np.array(img_offsets_x)
                img_offsets_y = np.array(img_offsets_y)
                color_map_x = np.empty([self.targetH, self.targetW, 3], dtype=int)
                color_map_y = np.empty([self.targetH, self.targetW, 3], dtype=int)
                for h_i in range(self.targetH):
                    for w_i in range(self.targetW):
                        color_map_x[h_i][w_i] = rgb_colors[int(img_offsets_x[h_i, w_i] / 256. * density_range)]
                        color_map_y[h_i][w_i] = rgb_colors[int(img_offsets_y[h_i, w_i] / 256. * density_range)]
                color_map_x = color_map_x.astype(np.uint8)
                color_map_y = color_map_y.astype(np.uint8)
                cv2.addWeighted(color_map_x, alpha, img_copy_x, 1 - alpha, 0, img_copy_x)
                cv2.addWeighted(color_map_y, alpha, img_copy_y, 1 - alpha, 0, img_copy_y)

                img_processed = x_rectified[i].data.cpu().mul_(0.5).add_(0.5)
                img_processed = to_pil_image(img_processed)
                img_processed = np.array(img_processed)
                if len(img_processed.shape) == 2:
                    img_processed = cv2.merge([img_processed.copy()] * 3)

                total_img = np.ones([self.targetH, self.targetW * 4 + 15, 3], dtype=int) * 255
                total_img[0:self.targetH, 0:self.targetW] = img
                total_img[0:self.targetH, self.targetW + 5:2 * self.targetW + 5] = img_copy_x
                total_img[0:self.targetH, self.targetW * 2 + 10:3 * self.targetW + 10] = img_copy_y
                total_img[0:self.targetH, self.targetW * 3 + 15:4 * self.targetW + 15] = img_processed
                total_img = cv2.resize(total_img.astype(np.uint8), (800, 100))
                # cv2.imshow("Input_Offsets_Output", total_img)
                # cv2.waitKey()
                self.log.image_summary('attention_map', [total_img], steps)
                # cv2.imwrite('attention_map', total_img)

            # return x_rectified, total_img
            return x_rectified

        return x_rectified
