import math
import numpy as np

from keras import backend as K

from keras.layers import Conv2D, Concatenate, Activation, Add
from keras.engine import InputSpec


def logsoftmax(x):
    ''' Numerically stable log(softmax(x)) '''
    m = K.max(x, axis=-1, keepdims=True)
    return x - m - K.log(K.sum(K.exp(x - m), axis=-1, keepdims=True))


def pixelcnn_loss(target, output, img_rows, img_cols, img_chns, n_components):
    ''' Keras PixelCNN loss function. Use a lambda to fill in the last few
        parameters

        Args:
            img_rows, img_cols, img_chns: image dimensions
            n_components: number of mixture components

        Returns:
            log-loss
    '''
    assert img_chns == 3

    # Extract out each of the mixture parameters (multiple of 3 b/c of image channels)
    output_m = output[:, :, :, :3*n_components]
    output_invs = output[:, :, :, 3*n_components:6*n_components]
    output_logit_weights = output[:, :, :, 6*(n_components):]

    # Repeat the target to match the number of mixture component shapes
    x = K.reshape(target, (-1, img_rows, img_cols, img_chns))
    slices = []
    for c in range(img_chns):
        slices += [x[:, :, :, c:c+1]] * n_components
    x = K.concatenate(slices, axis=-1)

    x_decoded_m = output_m
    x_decoded_invs = output_invs
    x_logit_weights = output_logit_weights

    # Pixels rescaled to be in [-1, 1] interval
    offset = 1. / 127.5 / 2.
    centered_mean = x - x_decoded_m

    cdfminus_arg = (centered_mean - offset) * K.exp(x_decoded_invs)
    cdfplus_arg = (centered_mean + offset) * K.exp(x_decoded_invs)

    cdfminus_safe = K.sigmoid(cdfminus_arg)
    cdfplus_safe = K.sigmoid(cdfplus_arg)

    # Generate the PDF (logistic) in case the `m` is way off (cdf is too small)
    # pdf = e^(-(x-m)/s) / {s(1 + e^{-(x-m)/s})^2}
    # logpdf = -(x-m)/s - log s - 2 * log(1 + e^(-(x-m)/s))
    #        = -mid_in - invs - 2 * softplus(-mid_in)
    mid_in = centered_mean * K.exp(x_decoded_invs)
    log_pdf_mid = -mid_in - x_decoded_invs - 2. * K.tf.nn.softplus(-mid_in)

    # Use trick from PixelCNN++ implementation to protect against edge/overflow cases
    # In extreme cases (cdfplus_safe - cdf_minus_safe < 1e-5), use the
    # log_pdf_mid and assume that density is 1 pixel width wide (1/127.5) as
    # the density: log(pdf * 1/127.5) = log(pdf) - log(127.5)
    # Add on line of best fit (see notebooks/blog post) to the difference between
    # edge case and the standard case
    edge_case = log_pdf_mid - np.log(127.5) + 2.04 * x_decoded_invs - 0.107

    # ln (sigmoid(x)) = x - ln(e^x + 1) = x - softplus(x)
    # ln (1 - sigmoid(x)) = ln(1 / (1 + e^x)) = -softplus(x)
    log_cdfplus = cdfplus_arg - K.tf.nn.softplus(cdfplus_arg)
    log_1minus_cdf = -K.tf.nn.softplus(cdfminus_arg)
    log_ll = K.tf.where(x <= -0.999, log_cdfplus,
                        K.tf.where(x >= 0.999, log_1minus_cdf,
                                   K.tf.where(cdfplus_safe - cdfminus_safe > 1e-5,
                                              K.log(K.maximum(cdfplus_safe - cdfminus_safe, 1e-12)),
                                              edge_case)))

    # x_weights * [sigma(x+0.5...) - sigma(x-0.5 ...) ]
    # = log x_weights + log (...)
    # Compute log(softmax(.)) directly here, instead of doing 2-step to avoid overflow
    pre_result = logsoftmax(x_logit_weights) + log_ll

    result = []
    for chn in range(img_chns):
        chn_result = pre_result[:, :, :, chn*n_components:(chn+1)*n_components]
        v = K.logsumexp(chn_result, axis=-1)
        result.append(v)
    result = K.batch_flatten(K.stack(result, axis=-1))

    return -K.sum(result, axis=-1)


def sigmoid(x):
    # Protect overflow
    if x < -20:
        return 0.0
    elif x > 20:
        return 1.0

    return 1 / (1 + math.exp(-x))


def logistic_cdf(x, loc, scale):
    return sigmoid((x - loc) / scale)


def compute_pvals(m, invs):
    pvals = []
    for i in range(256):
        if i == 0:
            pval = logistic_cdf((0.5 - 127.5) / 127.5, loc=m, scale=1. / np.exp(invs))
        elif i == 255:
            pval = 1. - logistic_cdf((254.5 - 127.5) / 127.5, loc=m, scale=1. / np.exp(invs))
        else:
            pval = (logistic_cdf((i + 0.5 - 127.5) / 127.5, loc=m, scale=1. / np.exp(invs))
                    - logistic_cdf((i - 0.5 - 127.5) / 127.5, loc=m, scale=1. / np.exp(invs)))
        pvals.append(pval)

    return pvals


def compute_mixture(ms, invs, weights, n_comps):
    components = []
    for i in range(n_comps):
        pvals = compute_pvals(ms[i], invs[i])
        arr = np.array(pvals)
        components.append(weights[i] * arr)
    return np.sum(components, axis=0)


class PixelConv2D(Conv2D):
    def __init__(self, ptype, *args, **kwargs):
        # ptype corresponds to pixel type and mask type, e.g. ra, ga, ba, rb, gb, bb
        assert ptype[0] in ['r', 'g', 'b'], ptype
        assert ptype[1] in ['a', 'b'], ptype
        self.ptype = ptype
        super(PixelConv2D, self).__init__(*args, **kwargs)

    def build_mask(self, kernel_shape):
        # kernel_shape = kern_dim x kern_dim x total_filters
        #              = kern_dim x kern_dim x r_g_b_filters x filters_per_channel
        assert kernel_shape[0] == kernel_shape[1], \
            "{} must be equal in first two dims".format(kernel_shape)
        assert kernel_shape[0] % 2 == 1, \
            "{} must be odd size in first two dims".format(kernel_shape)
        assert kernel_shape[2] % 3 == 0, \
            "{} must be divisible by 3".format(kernel_shape)
        data = np.ones(kernel_shape)

        data.shape
        mid = data.shape[0] // 2
        if self.ptype[0] == 'r':
            filt_prev = 0
            filt_thres = int(data.shape[2] / 3)
        elif self.ptype[0] == 'g':
            filt_prev = int(data.shape[2] / 3)
            filt_thres = int(2 * data.shape[2] / 3)
        else:
            assert self.ptype[0] == 'b', self.ptype
            filt_prev = int(2 * data.shape[2] / 3)
            filt_thres = data.shape[2]

        for k1 in range(data.shape[0]):
            for k2 in range(data.shape[1]):
                for chan in range(data.shape[2]):
                    if (self.ptype[1] == 'a'
                            and filt_prev <= chan < filt_thres
                            and k1 == mid and k2 == mid):
                        # Handle the only difference between 'a' and 'b' ptypes
                        data[k1, k2, chan, :] = 0
                    elif k1 > mid or (k1 >= mid and k2 > mid) or chan >= filt_thres:
                        # Turn off anything:
                        # a) Below currrent pixel
                        # b) Past the current pixel (scanning left from right, up to down)
                        # c) In a later filter
                        data[k1, k2, chan, :] = 0

        return K.constant(np.ravel(data), dtype='float32', shape=kernel_shape)

    def build(self, input_shape):
        if self.data_format == 'channels_first':
            channel_axis = 1
        else:
            channel_axis = -1
        if input_shape[channel_axis] is None:
            raise ValueError('The channel dimension of the inputs '
                             'should be defined. Found `None`.')
        input_dim = input_shape[channel_axis]
        kernel_shape = self.kernel_size + (input_dim, self.filters)

        self.kernel_mask = self.build_mask(kernel_shape)
        self.kernel = self.add_weight(shape=kernel_shape,
                                      initializer=self.kernel_initializer,
                                      name='kernel',
                                      regularizer=self.kernel_regularizer,
                                      constraint=self.kernel_constraint)
        if self.use_bias:
            self.bias = self.add_weight(shape=(self.filters,),
                                        initializer=self.bias_initializer,
                                        name='bias',
                                        regularizer=self.bias_regularizer,
                                        constraint=self.bias_constraint)
        else:
            self.bias = None
        # Set input spec.
        self.input_spec = InputSpec(ndim=self.rank + 2,
                                    axes={channel_axis: input_dim})
        self.built = True

    def call(self, inputs):
        masked_kernel = self.kernel * self.kernel_mask
        outputs = K.conv2d(
            inputs,
            masked_kernel,
            strides=self.strides,
            padding=self.padding,
            data_format=self.data_format,
            dilation_rate=self.dilation_rate)

        if self.use_bias:
            outputs = K.bias_add(
                outputs,
                self.bias,
                data_format=self.data_format)

        if self.activation is not None:
            return self.activation(outputs)

        return outputs


def conv_block(input_tensor, filters, kernel_size, name, is_first=False):
    outs = []
    for t in ['rb', 'gb', 'bb']:
        if is_first:
            t = t[0] + 'a'
        x = PixelConv2D(t, filters, kernel_size,
                        name='res' + name + t, padding='same')(input_tensor)
        x = Activation('relu')(x)
        outs.append(x)

    return Concatenate()(outs)


def resnet_block(input_tensor, filters, stage, block, kernel=3):
    name_base = str(stage) + block + '_branch'
    filters1, filters2, filters3 = filters
    x = input_tensor
    x = conv_block(x, filters1, (1, 1), name=name_base + '_a-1x1')
    x = conv_block(x, filters2, (kernel, kernel),
                   name=name_base + '_b-{}x{}'.format(kernel, kernel))
    x = conv_block(x, filters3, (1, 1), name=name_base + '_c-1x1')
    x = Add()([x, input_tensor])

    return x


def final_block(input_tensor, filters, in_filters, name, kernel_size=(1, 1)):
    outs = []
    for t in ['rb', 'gb', 'bb']:
        x = PixelConv2D(t, filters, kernel_size,
                        name='final' + name + '_' + t,
                        padding='same')(input_tensor)
        x = Activation('relu')(x)
        outs.append(x)

    return Concatenate()(outs)

