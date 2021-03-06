import tensorflow as tf
import numpy as np

from tensorflow.contrib import slim
from __main__ import opt
print("opt的值是  ",opt)
from yacs.config import CfgNode as CN

def read_config_file(config_file):
    # 用yaml重构配置文件
    f = open(config_file)
    opt_ = CN.load_cfg(f)
    return opt_

FLAGS = read_config_file(opt.config_file)
# tf.app.flags.DEFINE_integer('text_scale', 512, '')

from nets import resnet_v1

# FLAGS = tf.app.flags.FLAGS


def unpool(inputs):
    return tf.image.resize_bilinear(inputs, size=[tf.shape(inputs)[1]*2,  tf.shape(inputs)[2]*2])


def mean_image_subtraction(images, means=[115.99, 112.12, 106.28]):
    '''
    image normalization
    :param images:
    :param means:
    :return:
    '''
    num_channels = images.get_shape().as_list()[-1]
    if len(means) != num_channels:
      raise ValueError('len(means) must match the number of channels')
    channels = tf.split(axis=3, num_or_size_splits=num_channels, value=images)
    for i in range(num_channels):
        channels[i] -= means[i]
    return tf.concat(axis=3, values=channels)


def model(images, weight_decay=1e-5, is_training=True):
    '''
    define the model, we use slim's implemention of resnet
    '''
    images = mean_image_subtraction(images)

    with slim.arg_scope(resnet_v1.resnet_arg_scope(weight_decay=weight_decay)):
        logits, end_points = resnet_v1.resnet_v1_50(images, is_training=is_training, scope='resnet_v1_50')


    with tf.variable_scope('feature_fusion', values=[end_points.values]):
        batch_norm_params = {
        'decay': 0.997,
        'epsilon': 1e-5,
        'scale': True,
        'is_training': is_training
        }
        with slim.arg_scope([slim.conv2d],
                            activation_fn=tf.nn.relu,
                            normalizer_fn=slim.batch_norm,
                            normalizer_params=batch_norm_params,
                            weights_regularizer=slim.l2_regularizer(weight_decay)):
            f = [end_points['pool5'], end_points['pool4'],
                 end_points['pool3'], end_points['pool2']]
            for i in range(4):
                print('Shape of f_{} {}'.format(i, f[i].shape))
            g = [None, None, None, None]
            h = [None, None, None, None]
            num_outputs = [None, 128, 64, 32]
            for i in range(4):
                if i == 0:
                    h[i] = f[i]
                else:
                    c1_1 = slim.conv2d(tf.concat([g[i-1], f[i]], axis=-1), num_outputs[i], 1)
                    h[i] = slim.conv2d(c1_1, num_outputs[i], 3)
                if i <= 2:
                    g[i] = unpool(h[i])
                else:
                    g[i] = slim.conv2d(h[i], num_outputs[i], 3)
                print('Shape of h_{} {}, g_{} {}'.format(i, h[i].shape, i, g[i].shape))

            # here we use a slightly different way for regression part,
            # we first use a sigmoid to limit the regression range, and also
            # this is do with the angle map



            F_score_1 = slim.conv2d(g[3], 1, 1, activation_fn=tf.nn.sigmoid, normalizer_fn=None)
            # 4 channel of axis aligned bbox and 1 channel rotation angle


            geo_map_1 = slim.conv2d(g[3], 4, 1, activation_fn=tf.nn.sigmoid, normalizer_fn=None) * FLAGS.text_scale
            angle_map_1 = (slim.conv2d(g[3], 1, 1, activation_fn=tf.nn.sigmoid, normalizer_fn=None) - 0.5) * np.pi/2 # angle is between [-45, 45]
            F_geometry_1 = tf.concat([geo_map_1, angle_map_1], axis=-1)




            #增加一个预测层
            #print('h2',h[2].shape)
            p = slim.conv2d(h[2], num_outputs[2], 3)
            #print('p:',p.shape)
            F_score_2 = slim.conv2d(p, 1, 1, activation_fn=tf.nn.sigmoid, normalizer_fn=None)
            # 4 channel of axis aligned bbox and 1 channel rotation angle

            geo_map_2 = slim.conv2d(p, 4, 1, activation_fn=tf.nn.sigmoid, normalizer_fn=None) * FLAGS.text_scale
            angle_map_2 = (slim.conv2d(p, 1, 1, activation_fn=tf.nn.sigmoid,
                                     normalizer_fn=None) - 0.5) * np.pi / 2  # angle is between [-45, 45]
            F_geometry_2 = tf.concat([geo_map_2, angle_map_2], axis=-1)

            F_score = {'F_score1':F_score_1,
                        'F_score2':F_score_2}

            F_geometry = {'F_geometry1':F_geometry_1,
                          'F_geometry2':F_geometry_2}

            print('helloworld')
    return F_score, F_geometry

def dice_coefficient(y_true_cls, y_pred_cls,
                     training_mask):
    '''
    dice loss
    :param y_true_cls:
    :param y_pred_cls:
    :param training_mask:
    :return:
    '''
    eps = 1e-5
    intersection = tf.reduce_sum(y_true_cls * y_pred_cls * training_mask)
    union = tf.reduce_sum(y_true_cls * training_mask) + tf.reduce_sum(y_pred_cls * training_mask) + eps
    loss = 1. - (2 * intersection / union)
#     tf.summary.scalar('classification_dice_loss', loss)
    return loss


def loss(y_true_cls, y_pred_cls,
         y_true_geo, y_pred_geo,
         training_mask):
    '''
    define the loss used for training, contraning two part,
    the first part we use dice loss instead of weighted logloss,
    the second part is the iou loss defined in the paper
    :param y_true_cls: ground truth of text
    :param y_pred_cls: prediction os text
    :param y_true_geo: ground truth of geometry
    :param y_pred_geo: prediction of geometry
    :param training_mask: mask used in training, to ignore some text annotated by ###
    :return:
    '''
    
    
    classification_loss = dice_coefficient(y_true_cls, y_pred_cls, training_mask)

    # scale classification loss to match the iou loss part
    classification_loss *= 0.01

    # d1 -> top, d2->right, d3->bottom, d4->left
    d1_gt1, d2_gt1, d3_gt1, d4_gt1, theta_gt1 = tf.split(value=y_true_geo, num_or_size_splits=5, axis=3)
    d1_pred1, d2_pred1, d3_pred1, d4_pred1, theta_pred1 = tf.split(value=y_pred_geo, num_or_size_splits=5, axis=3)
    area_gt1 = (d1_gt1 + d3_gt1) * (d2_gt1 + d4_gt1)
    area_pred1 = (d1_pred1 + d3_pred1) * (d2_pred1 + d4_pred1)
    w_union1 = tf.minimum(d2_gt1, d2_pred1) + tf.minimum(d4_gt1, d4_pred1)
    h_union1 = tf.minimum(d1_gt1, d1_pred1) + tf.minimum(d3_gt1, d3_pred1)
    area_intersect1 = w_union1 * h_union1
    area_union1 = area_gt1 + area_pred1 - area_intersect1
    L_AABB1 = -tf.log((area_intersect1 + 1.0)/(area_union1 + 1.0))
    L_theta1 = 1 - tf.cos(theta_pred1 - theta_gt1)
#     tf.summary.scalar('geometry_AABB1', tf.reduce_mean(L_AABB1 * y_true_cls * training_mask))
#     tf.summary.scalar('geometry_theta1', tf.reduce_mean(L_theta1 * y_true_cls * training_mask))
    L_g1 = L_AABB1 + 20 * L_theta1


    return tf.reduce_mean(L_g1 * y_true_cls * training_mask) + classification_loss
