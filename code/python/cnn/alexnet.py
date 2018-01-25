'''
Copyright 2018 Supun Nakandala and Arun Kumar
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''
import os

import tensorflow as tf

from cnn_utils import conv, fc, max_pool, lrn, load_dict_from_hdf5


class AlexNet(object):

    transfer_layer_flattened_sizes = [227*227*3, 13 * 13 * 384, 13 * 13 * 256, 4096, 4096, 1000]
    transfer_layers_shapes = [(227, 227, 3), (13, 13, 384), (13, 13, 256), (1, 1, 4096), (1, 1, 4096), (1, 1, 1000)]

    def __init__(self, model_input, input_layer_name='image', model_name='alexnet', retrain_layers=False,
                 weights_path='DEFAULT'):
        self.model_input = model_input
        self.input_layer_name = input_layer_name
        self.model_name = model_name
        self.retrain_layers = retrain_layers

        if weights_path == 'DEFAULT':
            this_dir, _ = os.path.split(__file__)
            self.weights_path = os.path.join(this_dir, "resources", "alexnet_weights.h5")
        else:
            self.weights_path = weights_path

        # Call the create function to build the computational graph of AlexNet
        self.__create()

    def __create(self):
        with tf.variable_scope(self.model_name):

            self.weights_data = self.__get_weights_data()

            if self.input_layer_name == 'image':
                self.image = tf.reshape(tf.cast(self.model_input, tf.float32), [-1, 227, 227, 3])
                self.__preprocess_image()
                self.__calc_conv1()
                self.__calc_conv2()
                self.__calc_conv3()
                self.__calc_conv4()
                self.__calc_conv5()
                self.__calc_fc6()
                self.__calc_fc7()
                self.__calc_fc8()
                self.transfer_layers = [self.conv1, self.conv2, self.conv3, self.conv4, self.conv5, self.fc6, self.fc7,
                                        self.fc8]
            elif self.input_layer_name == 'conv4':
                self.conv4 = tf.reshape(tf.cast(self.model_input, tf.float32), [-1, 13, 13, 384])
                self.__calc_conv5()
                self.__calc_fc6()
                self.__calc_fc7()
                self.__calc_fc8()
                self.transfer_layers = [self.conv5, self.fc6, self.fc7, self.fc8]
            elif self.input_layer_name == 'conv5':
                self.conv5 = tf.reshape(tf.cast(self.model_input, tf.float32), [-1, 13, 13, 256])
                # have to take the pool first before feeding to fc6
                self.pool5 = max_pool(self.conv5, 3, 3, 2, 2, padding='VALID', name='pool5')
                self.__calc_fc6()
                self.__calc_fc7()
                self.__calc_fc8()
                self.transfer_layers = [self.fc6, self.fc7, self.fc8]
            elif self.input_layer_name == 'fc6':
                self.fc6 = tf.reshape(tf.cast(self.model_input, tf.float32), [-1, 4096])
                self.__calc_fc7()
                self.__calc_fc8()
                self.transfer_layers = [self.fc7, self.fc8]
            elif self.input_layer_name == 'fc7':
                # 8th Layer: FC and return unscaled activations
                self.fc7 = tf.reshape(tf.cast(self.model_input, tf.float32), [-1, 4096])
                self.__calc_fc8()
                self.transfer_layers = [self.fc8]
            else:
                raise Exception('invalid input layer name... ' + self.input_layer_name)

            self.probs = tf.nn.softmax(self.fc8, name='softmax')


    def __preprocess_image(self):
        # zero-mean input
        with tf.variable_scope('preprocess'):
            mean = tf.constant([104., 117., 124.], dtype=tf.float32, shape=[1, 1, 1, 3], name='img_mean')
            self.preprocessed_image = self.image - mean
            # 'RGB'->'BGR'
            self.preprocessed_image = self.preprocessed_image[:, :, :, ::-1]

    def __calc_conv1(self):
        # 1st layer: conv (w relu) -> pool -> lrn
        self.conv1 = tf.nn.relu(conv(self.preprocessed_image, 11, 11, 96, 4, 4, padding='VALID', name='conv1',
                                     data=self.weights_data,
                                     retrain_layers=self.retrain_layers))
        self.pool1 = max_pool(self.conv1, 3, 3, 2, 2, padding='VALID', name='pool1')
        self.norm1 = lrn(self.pool1, 2, 2e-05, 0.75, name='norm1')

    def __calc_conv2(self):
        # 2nd Layer: Conv (w ReLu) -> Pool -> Lrn with 2 groups
        self.conv2 = tf.nn.relu(conv(self.norm1, 5, 5, 256, 1, 1, name='conv2', groups=2,
                                     data=self.weights_data,
                                     retrain_layers=self.retrain_layers))
        self.pool2 = max_pool(self.conv2, 3, 3, 2, 2, padding='VALID', name='pool2')
        self.norm2 = lrn(self.pool2, 2, 2e-05, 0.75, name='norm2')

    def __calc_conv3(self):
        # 3rd Layer: Conv (w ReLu)
        self.conv3 = tf.nn.relu(conv(self.norm2, 3, 3, 384, 1, 1, name='conv3',
                                     data=self.weights_data,
                                     retrain_layers=self.retrain_layers))

    def __calc_conv4(self):
        # 4th Layer: Conv (w ReLu) splitted into two groups
        self.conv4 = tf.nn.relu(conv(self.conv3, 3, 3, 384, 1, 1, name='conv4', groups=2,
                                     data=self.weights_data,
                                     retrain_layers=self.retrain_layers))

    def __calc_conv5(self):
        # 5th Layer: Conv (w ReLu) -> Pool splitted into two groups
        self.conv5 = tf.nn.relu(conv(self.conv4, 3, 3, 256, 1, 1, name='conv5', groups=2,
                                     data=self.weights_data,
                                     retrain_layers=self.retrain_layers))
        self.pool5 = max_pool(self.conv5, 3, 3, 2, 2, padding='VALID', name='pool5')

    def __calc_fc6(self):
        # 6th Layer: Flatten -> FC (w ReLu)
        flattened = tf.reshape(self.pool5, [-1, 6 * 6 * 256])
        self.fc6 = tf.nn.relu(fc(flattened, 6 * 6 * 256, 4096, name='fc6',
                                 data=self.weights_data,
                                 retrain_layers=self.retrain_layers))

    def __calc_fc7(self):
        # 7th Layer: FC (w ReLu)
        self.fc7 = tf.nn.relu(fc(self.fc6, 4096, 4096, name='fc7',
                                 data=self.weights_data,
                                 retrain_layers=self.retrain_layers))

    def __calc_fc8(self):
        # 8th Layer: FC and return unscaled activations
        self.fc8 = fc(self.fc7, 4096, 1000, name='fc8', data=self.weights_data)

    def __get_weights_data(self):
        # Load the weights and biases into memory
        return load_dict_from_hdf5(self.weights_path)

    def load_initial_weights(self, session):
        if not self.retrain_layers:
            raise Exception('retraining convnet is not enabled')

        # Load the weights into memory
        weights_dict = self.__get_weights_data()

        # Loop over all layer names stored in the weights dict
        for k in weights_dict:
            if len(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.model_name + "/" + k)) != 0:
                with tf.variable_scope(self.model_name + "/" + k, reuse=True):
                    data = weights_dict[k]
                    for var_name in data:
                        # Biases
                        if str(var_name).endswith("_b:0"):
                            var = tf.get_variable('biases')
                            session.run(var.assign(data[var_name]))
                        # Weights
                        else:
                            var = tf.get_variable('weights')
                            session.run(var.assign(data[var_name]))

    @staticmethod
    def get_transfer_learning_layer_names():
        return ['image','conv4', 'conv5', 'fc6', 'fc7', 'fc8']
