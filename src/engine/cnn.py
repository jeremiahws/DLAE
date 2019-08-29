# Copyright 2019 Jeremiah Sanders.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""src/engine/cnn.py

Constructs the convolutional neural network technique of DLAE.
"""


import tensorflow as tf
from src.utils.engine_utils import *
from src.utils.general_utils import write_hdf5
from keras.utils import to_categorical
from sklearn.utils import class_weight
import numpy as np
import datetime
import time
from keras.preprocessing.image import ImageDataGenerator
from src.utils.data_utils import ImageDataGenerator3D
from math import floor


class ConvolutionalNeuralNetwork(object):
    def __init__(self, engine_configs):
        self.engine_configs = engine_configs
        self.graph = tf.get_default_graph()
        self.errors = []
        self.model = None
        self.parallel_model = None

    def construct_graph(self):
        if self.engine_configs.train_options.s_hardware == "cpu" or self.engine_configs.train_options.s_hardware == "multi-gpu":
            device = "/cpu:0"
        else:
            device = "/gpu:0"

        with tf.device(device):
            if any(self.engine_configs.layers.s_listOfLayers):
                inputs, outputs, _, errors = get_io(self.engine_configs.layers.s_listOfLayers)
                if any(errors):
                    [self.errors.append(error) for error in errors]
                else:
                    try:
                        self.model = keras.models.Model(inputs=inputs, outputs=outputs)
                    except:
                        self.errors.append('Level3Error:CouldNotConstructCnnGraph')

            else:
                self.errors.append('Level3Error:NoListofSerialLayersFound')

    def compile_graph(self):
        if self.engine_configs.train_options.i_nGpus > 1:
            try:
                self.parallel_model = ModelMGPU(self.model, self.engine_configs.train_options.i_nGpus)
            except:
                self.errors.append('Level3Error:CouldNotConvertCnnModeltoMultiGpuModel')

            try:
                self.parallel_model.compile(optimizer=self.engine_configs.optimizer.optimizer,
                                            loss=self.engine_configs.loss_function.loss,
                                            metrics=self.engine_configs.monitors.monitors)
            except:
                self.errors.append('Level3Error:CouldNotCompileMultiGpuCnnGraph')
        else:
            try:
                self.model.compile(optimizer=self.engine_configs.optimizer.optimizer,
                                   loss=self.engine_configs.loss_function.loss,
                                   metrics=self.engine_configs.monitors.monitors)
            except:
                self.errors.append('Level3Error:CouldNotCompileCnnGraph')

    def train_graph(self):
        if self.engine_configs.data_preprocessing.b_weight_loss:
            try:
                class_weights = class_weight.compute_class_weight('balanced',
                                                                  np.unique(self.engine_configs.train_data.trainY),
                                                                  self.engine_configs.train_data.trainY)
            except:
                self.errors.append('Level3Error:CouldNotComputeCnnClassWeights')
        else:
            class_weights = None

        if self.engine_configs.data_preprocessing.b_to_categorical:
            try:
                self.engine_configs.train_data.trainY = to_categorical(self.engine_configs.train_data.trainY)
            except ValueError:
                self.errors.append('Level3Error:CouldNotConvertTrainYtoCategorical')

            if self.engine_configs.val_data.valY is not None:
                try:
                    self.engine_configs.val_data.valY = to_categorical(self.engine_configs.val_data.valY)
                except ValueError:
                    self.errors.append('Level3Error:CouldNotConvertValidationYtoCategorical')

        try:
            if self.engine_configs.val_data.valX is not None:
                val_data = (self.engine_configs.val_data.valX, self.engine_configs.val_data.valY)
                val_split = 0.0
            elif self.engine_configs.val_data.valX is None and self.engine_configs.train_options.f_validationSplit > 0:
                val_split = self.engine_configs.train_options.f_validationSplit
                n_tot_imgs = self.engine_configs.train_data.trainX.shape[0]
                n_val_imgs = floor(val_split * n_tot_imgs)
                self.engine_configs.val_data.valX = self.engine_configs.train_data.trainX[:n_val_imgs]
                self.engine_configs.val_data.valY = self.engine_configs.train_data.trainY[:n_val_imgs]
                self.engine_configs.train_data.trainX = self.engine_configs.train_data.trainX[n_val_imgs:]
                self.engine_configs.train_data.trainY = self.engine_configs.train_data.trainY[n_val_imgs:]
                val_data = (self.engine_configs.val_data.valX, self.engine_configs.val_data.valY)
            else:
                val_data = None
                val_split = 0.0
            val_steps = None

            if val_split > 0.0:
                from sklearn.model_selection import train_test_split
                X_train, X_val, y_train, y_val = train_test_split(self.engine_configs.train_data.trainX,
                                                                  self.engine_configs.train_data.trainY,
                                                                  test_size=val_split,
                                                                  stratify=self.engine_configs.train_data.trainY)

                self.engine_configs.train_data.trainX = np.concatenate((X_train, X_val))
                self.engine_configs.train_data.trainY = np.concatenate((y_train, y_val))

        except:
            self.errors.append('Level3Error:CouldNotDetermineCnnValidationData')

        if self.engine_configs.augmentation.b_augmentation:
            if self.engine_configs.data_preprocessing.s_image_context == '2D':
                try:
                    X_data = ImageDataGenerator(featurewise_center=self.engine_configs.augmentation.b_fw_centering,
                                                samplewise_center=self.engine_configs.augmentation.b_sw_centering,
                                                featurewise_std_normalization=self.engine_configs.augmentation.b_fw_normalization,
                                                samplewise_std_normalization=self.engine_configs.augmentation.b_sw_normalization,
                                                rotation_range=self.engine_configs.augmentation.i_rotation_range,
                                                width_shift_range=self.engine_configs.augmentation.f_width_shift,
                                                height_shift_range=self.engine_configs.augmentation.f_height_shift,
                                                brightness_range=self.engine_configs.augmentation.t_brightness_range,
                                                shear_range=self.engine_configs.augmentation.f_shear_range,
                                                zoom_range=self.engine_configs.augmentation.f_zoom_range,
                                                channel_shift_range=self.engine_configs.augmentation.f_channel_shift_range,
                                                fill_mode=self.engine_configs.augmentation.s_fill_mode,
                                                cval=self.engine_configs.augmentation.f_cval,
                                                horizontal_flip=self.engine_configs.augmentation.b_horizontal_flip,
                                                vertical_flip=self.engine_configs.augmentation.b_vertical_flip)

                except:
                    self.errors.append('Level3Error:CouldNotEstablish2dDataAugmentationGenerator')

            elif self.engine_configs.data_preprocessing.s_image_context == '3D':
                try:
                    X_data = ImageDataGenerator3D(featurewise_center=self.engine_configs.augmentation.b_fw_centering,
                                                  samplewise_center=self.engine_configs.augmentation.b_sw_centering,
                                                  featurewise_std_normalization=self.engine_configs.augmentation.b_fw_normalization,
                                                  samplewise_std_normalization=self.engine_configs.augmentation.b_sw_normalization,
                                                  rotation_range=self.engine_configs.augmentation.i_rotation_range,
                                                  width_shift_range=self.engine_configs.augmentation.f_width_shift,
                                                  height_shift_range=self.engine_configs.augmentation.f_height_shift,
                                                  shear_range=self.engine_configs.augmentation.f_shear_range,
                                                  zoom_range=self.engine_configs.augmentation.f_zoom_range,
                                                  channel_shift_range=self.engine_configs.augmentation.f_channel_shift_range,
                                                  fill_mode=self.engine_configs.augmentation.s_fill_mode,
                                                  cval=self.engine_configs.augmentation.f_cval,
                                                  horizontal_flip=self.engine_configs.augmentation.b_horizontal_flip,
                                                  vertical_flip=self.engine_configs.augmentation.b_vertical_flip)

                except:
                    self.errors.append('Level3Error:CouldNotEstablish3dDataAugmentationGenerator')

            try:
                X_data.fit(self.engine_configs.train_data.trainX, rounds=2, seed=1)
                train_generator = X_data.flow(self.engine_configs.train_data.trainX,
                                              self.engine_configs.train_data.trainY,
                                              batch_size=self.engine_configs.train_options.i_batchSize)

            except:
                self.errors.append('Level3Error:CouldNotConstructDataAugmentationGenerator')

        with self.graph.as_default():
            if self.engine_configs.train_options.i_nGpus > 1:
                try:
                    if self.engine_configs.augmentation.b_augmentation:
                        self.parallel_model.fit_generator(train_generator,
                                                          epochs=self.engine_configs.train_options.i_epochs,
                                                          validation_data=val_data,
                                                          steps_per_epoch=np.ceil(2 * self.engine_configs.train_data.trainX.shape[0] * (1. - val_split) / self.engine_configs.train_options.i_batchSize),
                                                          validation_steps=val_steps,
                                                          shuffle=self.engine_configs.train_options.b_shuffleData,
                                                          callbacks=self.engine_configs.callbacks.callbacks,
                                                          class_weight=class_weights)
                    else:
                        self.parallel_model.fit(self.engine_configs.train_data.trainX,
                                                self.engine_configs.train_data.trainY,
                                                batch_size=self.engine_configs.train_options.i_batchSize,
                                                epochs=self.engine_configs.train_options.i_epochs,
                                                validation_split=val_split,
                                                validation_data=val_data,
                                                shuffle=self.engine_configs.train_options.b_shuffleData,
                                                callbacks=self.engine_configs.callbacks.callbacks,
                                                class_weight=class_weights)
                except:
                    self.errors.append('Level3Error:CouldNotFitCnnGraphwithMultiGpu')
            else:
                try:
                    if self.engine_configs.augmentation.b_augmentation:
                        self.model.fit_generator(train_generator,
                                                 epochs=self.engine_configs.train_options.i_epochs,
                                                 validation_data=val_data,
                                                 steps_per_epoch=np.ceil(2 * self.engine_configs.train_data.trainX.shape[0] * (1. - val_split) / self.engine_configs.train_options.i_batchSize),
                                                 validation_steps=val_steps,
                                                 shuffle=self.engine_configs.train_options.b_shuffleData,
                                                 callbacks=self.engine_configs.callbacks.callbacks,
                                                 class_weight=class_weights)
                    else:
                        self.model.fit(self.engine_configs.train_data.trainX, self.engine_configs.train_data.trainY,
                                       batch_size=self.engine_configs.train_options.i_batchSize,
                                       epochs=self.engine_configs.train_options.i_epochs,
                                       validation_split=val_split,
                                       validation_data=val_data,
                                       shuffle=self.engine_configs.train_options.b_shuffleData,
                                       callbacks=self.engine_configs.callbacks.callbacks,
                                       class_weight=class_weights)
                except:
                    self.errors.append('Level3Error:CouldNotFitCnnGraph')

        if self.engine_configs.saver.b_saveModel:
            try:
                self.model.save(self.engine_configs.saver.s_saveModelPath)
            except:
                self.errors.append('Level3Error:NoCnnGraphtoSave')

    def retrain_graph(self):
        pass

    def predict_on_graph(self):
        if self.engine_configs.data_preprocessing.b_to_categorical:
            try:
                self.engine_configs.test_data.testY = to_categorical(self.engine_configs.test_data.testY)
            except ValueError:
                self.errors.append('Level3Error:CouldNotConvertTestYtoCategorical')

        try:
            self.model = keras.models.load_model(self.engine_configs.loader.s_loadModelPath)
            predictions = self.model.predict(self.engine_configs.test_data.testX)

            stamp = datetime.datetime.fromtimestamp(time.time()).strftime('date_%Y_%m_%d_time_%H_%M_%S')

            write_hdf5('cnn_predictions_' + stamp + '.h5', predictions)
        except:
            self.errors.append('Level3Error:CouldNotMakePredictionsonCnnGraph')
