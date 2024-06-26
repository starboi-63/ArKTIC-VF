import torch
import torch.nn as nn
from model.helper_modules import MySequential, Conv_2d


class ChronoSynth(nn.Module):
    def __init__(self, num_inputs, num_features, kernel_size, dilation, apply_softmax=True):
        super(ChronoSynth, self).__init__()

        num_features_with_time = num_features + 1
        # Subnetwork to learn vertical and horizontal offsets during convolution
        def Subnet_offset(kernel_size):
            return MySequential(
                nn.Conv2d(
                    in_channels=num_features_with_time, out_channels=num_features, kernel_size=3, stride=1, padding=1),
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
                    in_channels=num_features_with_time, out_channels=num_features, kernel_size=3, stride=1, padding=1),
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
                    in_channels=num_features_with_time, out_channels=num_features, kernel_size=3, stride=1, padding=1),
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
        self.ModuleOcclusion = Subnet_occlusion()

        self.feature_fuse = Conv_2d(
            num_features_with_time * num_inputs, num_features_with_time, kernel_size=1, stride=1, batchnorm=False, bias=True)
        self.lrelu = nn.LeakyReLU(0.2)

    def forward(self, features, frames, output_size, output_frame_times):
        """
        output_frame_times: batch of arbitrary 't' from 0 to 1
        We create the time scalar from the dimensions of the input feature 
        """
        H, W = output_size

        B, C, T, cur_H, cur_W = features.shape

        # Create a tensor which will add 1 extra channel representing the time of context frames
        time_tensor = torch.ones((B, 1, T, cur_H, cur_W)).to(features.device)

        # NOTE: For training, since we are using the septuplet dataset, our frame times are -0.25, 0, 1, 1.25

        # -------------------------------------------------------------------------------------------
        context_frame_times = [-0.25, 0, 1, 1.25]
        # Example: goes from -1, 0, 1, 2 for T = 4
        start, end = -T//2 + 1, T//2 + 1
        for i in range(start, end):
            time_differences = torch.abs(context_frame_times[i] - output_frame_times)
            time_tensor[:, :, i - start, :, :] *= time_differences.view(B, 1, 1, 1)
        # -------------------------------------------------------------------------------------------

        # Concatenate the time tensor to the channel dimension of the features
        features = torch.cat([features, time_tensor], 1)
        occ = torch.cat(torch.unbind(features, 1), 1)
        occ = self.lrelu(self.feature_fuse(occ))

        # Reshape the features so that the synthesis module can solely utilize CxHxW
        features = features.transpose(1, 2).reshape(B*T, C + 1, cur_H, cur_W)
        # Recover the temporal dimension
        weights = self.ModuleWeight(features, (H, W)).view(B, T, -1, H, W)
        alphas = self.ModuleAlpha(features, (H, W)).view(B, T, -1, H, W)
        betas = self.ModuleBeta(features, (H, W)).view(B, T, -1, H, W)
        occlusion = self.ModuleOcclusion(occ, (H, W)) 

        warp = []
        for i in range(self.num_inputs):
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
