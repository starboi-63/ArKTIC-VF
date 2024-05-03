import torch
import torch.nn as nn
from model.helper_modules import MySequential, Conv_2d


class ChronoSynth(nn.Module):
    def __init__(self, num_inputs, num_features, kernel_size, dilation, delta_t, apply_softmax=True):
        super(ChronoSynth, self).__init__()

        self.delta_t = delta_t

        # Subnetwork to learn vertical and horizontal offsets during convolution
        def Subnet_offset(kernel_size):
            return MySequential(
                nn.Conv2d(
                    in_channels=num_features, out_channels=num_features, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.Conv2d(
                    in_channels=num_features, out_channels=kernel_size, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.ConvTranspose2d(
                    kernel_size, kernel_size, kernel_size=3, stride=2, padding=1),
                nn.Conv2d(
                    in_channels=kernel_size, out_channels=kernel_size, kernel_size=3, stride=1, padding=1)
            )

        # Subnetwork to learn weights for each pixel in the kernel
        def Subnet_weight(kernel_size):
            return MySequential(
                nn.Conv2d(
                    in_channels=num_features, out_channels=num_features, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.Conv2d(
                    in_channels=num_features, out_channels=kernel_size, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.ConvTranspose2d(
                    kernel_size, kernel_size, kernel_size=3, stride=2, padding=1),
                nn.Conv2d(
                    in_channels=kernel_size, out_channels=kernel_size, kernel_size=3, stride=1, padding=1),
                nn.Softmax(1) if apply_softmax else nn.Identity()
            )

        # Subnetwork to learn occlusion masks
        def Subnet_occlusion():
            return MySequential(
                nn.Conv2d(
                    in_channels=num_features, out_channels=num_features, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.Conv2d(
                    in_channels=num_features, out_channels=num_features, kernel_size=3, stride=1, padding=1),
                nn.LeakyReLU(negative_slope=0.2, inplace=False),
                nn.ConvTranspose2d(
                    num_features, num_features, kernel_size=3, stride=2, padding=1),
                nn.Conv2d(
                    in_channels=num_features, out_channels=num_inputs, kernel_size=3, stride=1, padding=1),
                nn.Softmax(dim=1)
            )

        self.num_inputs = num_inputs
        self.kernel_size = kernel_size
        self.kernel_pad = int(((kernel_size - 1) * dilation) / 2.0)
        self.dilation = dilation

        self.modulePad = nn.ReplicationPad2d(
            [self.kernel_pad, self.kernel_pad, self.kernel_pad, self.kernel_pad])

        import cupy_module.synth as synth
        self.moduleSynth = synth.FunctionSynth.apply

        self.ModuleWeight = Subnet_weight(kernel_size ** 2)
        self.ModuleAlpha = Subnet_offset(kernel_size ** 2)
        self.ModuleBeta = Subnet_offset(kernel_size ** 2)
        self.moduleOcclusion = Subnet_occlusion()

        self.feature_fuse = Conv_2d(
            num_features * num_inputs, num_features, kernel_size=1, stride=1, batchnorm=False, bias=True)
        self.lrelu = nn.LeakyReLU(0.2)

    def forward(self, features, frames, output_size, time_scalar):
        """
        time_scalar: a value 't' from 0 to 1 that represents the arbitrary time between frames
        We create the time scalar from the dimensions of the input feature 
        """
        H, W = output_size

        unbound = torch.unbind(features, 1)
        print("Unbound len: ", len(unbound))

        print("Unbound shape : ", type(unbound[0]))
        time_tensor = torch.ones((1, 1, features.shape[3], features.shape[4])).to(features.device) * time_scalar
        print("features shape", features.shape)
        print("time tensor shape", time_tensor.shape)

        F0, F1, F2, F3 = unbound

        F0 = torch.cat([F0, time_tensor + self.delta_t])
        F1 = torch.cat([F1, time_tensor])
        F2 = torch.cat([F2, 1 - time_tensor])
        F3 = torch.cat([F3, 1 - time_tensor + self.delta_t])

        occ = torch.cat([F0, F1, F2, F3], 1)
        occ = self.lrelu(self.feature_fuse(occ))
        occlusion = self.moduleOcclusion(occ, (H, W)) 

        B, C, T, cur_H, cur_W = features.shape
        features = features.transpose(1, 2).reshape(B*T, C, cur_H, cur_W)
        print("THIS FEATURES SHAPE: ", features.shape)
        weights = self.ModuleWeight(features, (H, W)).view(B, T, -1, H, W)
        alphas = self.ModuleAlpha(features, (H, W)).view(B, T, -1, H, W)
        betas = self.ModuleBeta(features, (H, W)).view(B, T, -1, H, W)

        warp = []
        for i in range(self.n_inputs):
            weight = weights[:, i].contiguous()
            alpha = alphas[:, i].contiguous()
            beta = betas[:, i].contiguous()
            occ = occlusion[:, i:i+1] 
            frame = nn.functional.interpolate(
                frames[i], size=weight.size()[-2:], mode='bilinear')

            warp.append(
                occ *
                self.moduleSynth(self.modulePad(frame),
                                  weight, alpha, beta, self.dilation)
            )

        framet = sum(warp)
        return framet
