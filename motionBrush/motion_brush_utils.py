import torch
import numpy as np
from typing import Any, Optional, Tuple, Union
from diffusers.schedulers.scheduling_euler_discrete import EulerDiscreteScheduler, EulerDiscreteSchedulerOutput, logger
from diffusers.utils.torch_utils import randn_tensor
from diffusers import StableVideoDiffusionPipeline
from PIL import Image
from image_utils import make_gif
from omegaconf import OmegaConf
from transformers import CLIPVisionModelWithProjection, CLIPImageProcessor

from easyanimate.pipeline.pipeline_easyanimate_inpaint import EasyAnimateInpaintPipeline
from predict_i2v import model_name, weight_dtype, scheduler_dict, low_gpu_memory_mode


class EulerDiscreteSchedulerMotionBrush(EulerDiscreteScheduler):
    def __init__(self, *args, mask=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask = mask

    def replace_prediction_with_mask(self, prediction, mask):
        '''
        for frames from 2 to end, replace the region where mask == 0 with the prediction of frame 1
        '''
        if mask is None:
            return prediction
        *_, height, width = prediction.shape
        if isinstance(mask, np.ndarray):
            mask = torch.from_numpy(mask).to(prediction.device)
        mask = mask.squeeze()
        while mask.dim() < prediction.dim() - 1:
            mask = mask.unsqueeze(0)
        resized_mask = torch.nn.functional.interpolate(mask, size=(height, width), mode="bilinear").unsqueeze(0)

        prediction[:, 1:] = torch.where(resized_mask > 0.5, prediction[:, 1:], prediction[:, 0:1])
        return prediction

    def step(
        self,
        model_output: torch.FloatTensor,
        timestep: Union[float, torch.FloatTensor],
        sample: torch.FloatTensor,
        s_churn: float = 0.0,
        s_tmin: float = 0.0,
        s_tmax: float = float("inf"),
        s_noise: float = 1.0,
        generator: Optional[torch.Generator] = None,
        return_dict: bool = True,
    ) -> Union[EulerDiscreteSchedulerOutput, Tuple]:
        """
        Predict the sample from the previous timestep by reversing the SDE. This function propagates the diffusion
        process from the learned model outputs (most often the predicted noise).

        Args:
            model_output (`torch.FloatTensor`):
                The direct output from learned diffusion model.
            timestep (`float`):
                The current discrete timestep in the diffusion chain.
            sample (`torch.FloatTensor`):
                A current instance of a sample created by the diffusion process.
            s_churn (`float`):
            s_tmin  (`float`):
            s_tmax  (`float`):
            s_noise (`float`, defaults to 1.0):
                Scaling factor for noise added to the sample.
            generator (`torch.Generator`, *optional*):
                A random number generator.
            return_dict (`bool`):
                Whether or not to return a [`~schedulers.scheduling_euler_discrete.EulerDiscreteSchedulerOutput`] or
                tuple.

        Returns:
            [`~schedulers.scheduling_euler_discrete.EulerDiscreteSchedulerOutput`] or `tuple`:
                If return_dict is `True`, [`~schedulers.scheduling_euler_discrete.EulerDiscreteSchedulerOutput`] is
                returned, otherwise a tuple is returned where the first element is the sample tensor.
        """

        if (
            isinstance(timestep, int)
            or isinstance(timestep, torch.IntTensor)
            or isinstance(timestep, torch.LongTensor)
        ):
            raise ValueError(
                (
                    "Passing integer indices (e.g. from `enumerate(timesteps)`) as timesteps to"
                    " `EulerDiscreteScheduler.step()` is not supported. Make sure to pass"
                    " one of the `scheduler.timesteps` as a timestep."
                ),
            )

        if not self.is_scale_input_called:
            logger.warning(
                "The `scale_model_input` function should be called before `step` to ensure correct denoising. "
                "See `StableDiffusionPipeline` for a usage example."
            )

        if self.step_index is None:
            self._init_step_index(timestep)

        sigma = self.sigmas[self.step_index]

        gamma = min(s_churn / (len(self.sigmas) - 1), 2**0.5 - 1) if s_tmin <= sigma <= s_tmax else 0.0

        noise = randn_tensor(
            model_output.shape, dtype=model_output.dtype, device=model_output.device, generator=generator
        )

        eps = noise * s_noise
        sigma_hat = sigma * (gamma + 1)

        if gamma > 0:
            sample = sample + eps * (sigma_hat**2 - sigma**2) ** 0.5

        # 1. compute predicted original sample (x_0) from sigma-scaled predicted noise
        # NOTE: "original_sample" should not be an expected prediction_type but is left in for
        # backwards compatibility
        if self.config.prediction_type == "original_sample" or self.config.prediction_type == "sample":
            pred_original_sample = model_output
        elif self.config.prediction_type == "epsilon":
            pred_original_sample = sample - sigma_hat * model_output
        elif self.config.prediction_type == "v_prediction":
            # denoised = model_output * c_out + input * c_skip
            pred_original_sample = model_output * (-sigma / (sigma**2 + 1) ** 0.5) + (sample / (sigma**2 + 1))
        else:
            raise ValueError(
                f"prediction_type given as {self.config.prediction_type} must be one of `epsilon`, or `v_prediction`"
            )

        pred_original_sample = self.replace_prediction_with_mask(pred_original_sample, self.mask)

        # 2. Convert to an ODE derivative
        derivative = (sample - pred_original_sample) / sigma_hat

        dt = self.sigmas[self.step_index + 1] - sigma_hat

        prev_sample = sample + derivative * dt

        # upon completion increase step index by one
        self._step_index += 1

        if not return_dict:
            return (prev_sample,)

        return EulerDiscreteSchedulerOutput(prev_sample=prev_sample, pred_original_sample=pred_original_sample)

class MotionBrush():
    def __init__(self):
        self.pipe = None

    def _init_pipe(self):
        if self.pipe is None:
            # scheduler = EulerDiscreteSchedulerMotionBrush.from_pretrained("stabilityai/stable-video-diffusion-img2vid-xt", subfolder='scheduler')
            # pipe = StableVideoDiffusionPipeline.from_pretrained(
            #     "stabilityai/stable-video-diffusion-img2vid-xt", torch_dtype=torch.float16, variant="fp16",
            #     scheduler=scheduler
            # )
            # easyAnimate添加motionBrush控制
            clip_image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                model_name, subfolder="image_encoder"
            ).to("cuda", weight_dtype)
            clip_image_processor = CLIPImageProcessor.from_pretrained(
                model_name, subfolder="image_encoder"
            )
            pipeline = EasyAnimateInpaintPipeline(
                vae=self.vae,
                text_encoder=self.text_encoder,
                tokenizer=self.tokenizer,
                transformer=self.transformer,
                scheduler=scheduler_dict["EulerMotionBrush"](
                    **OmegaConf.to_container(self.inference_config.noise_scheduler_kwargs)),
                clip_image_encoder=clip_image_encoder,
                clip_image_processor=clip_image_processor,
            )
            if low_gpu_memory_mode:
                pipeline.enable_sequential_cpu_offload()
            else:
                pipeline.enable_model_cpu_offload()
            print("Update diffusion transformer done")
            # pipe.enable_model_cpu_offload()
            self.pipe = pipeline

    def __call__(self, image, mask):
        if self.pipe is None:
            self._init_pipe()

        self.pipe.scheduler.mask = mask

        generator = torch.manual_seed(4)
        with torch.cuda.amp.autocast(dtype=torch.float16):
            frames = self.pipe(
                Image.fromarray(image),
                decode_chunk_size=6,
                generator=generator,
                num_frames=25,
                motion_bucket_id=100,
                num_inference_steps=25,
            ).frames
        frames_np = [np.array(frame) for frame in frames[0]]
        make_gif(frames_np, "result.gif", fps=8, rescale=0.5)
        return "result.gif"