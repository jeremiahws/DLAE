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
"""src/engine/bbd.py

Constructs the bounding box detector technique of DLAE.
"""


import tensorflow as tf
from src.utils.engine_utils import *
import datetime
import time
from src.utils.general_utils import write_hdf5
from src.utils.ssd_utils import *
from math import floor


class BoundingBoxDetector(object):
    def __init__(self, engine_configs):
        self.engine_configs = engine_configs
        self.graph = tf.get_default_graph()
        self.errors = []
        self.warnings = []
        self.model = None
        self.parallel_model = None
        self.input_encoder = None

    def construct_graph(self):
        if self.engine_configs.train_options.s_hardware == "cpu"\
                or self.engine_configs.train_options.s_hardware == "multi-gpu":
            device = "/cpu:0"
        else:
            device = "/gpu:0"

        with tf.device(device):
            inputs, outputs, hooks, errors = get_io(self.engine_configs.layers.s_listOfLayers)
            n_predictor_layers = len(hooks)
            if self.engine_configs.train_options.s_aspectRatiosType == "global":
                global_aspect_ratios = self.engine_configs.train_options.l_aspectRatios
                aspect_ratios = [global_aspect_ratios] * n_predictor_layers
                aspect_ratios_per_layer = None
            else:
                global_aspect_ratios = None
                aspect_ratios_per_layer = self.engine_configs.train_options.l_aspectRatios
                aspect_ratios = aspect_ratios_per_layer

            if aspect_ratios_per_layer is None:
                if (1.0 in global_aspect_ratios) and self.engine_configs.train_options.b_twoBoxesForAR1:
                    n_boxes = len(global_aspect_ratios) + 1
                else:
                    n_boxes = len(global_aspect_ratios)
                n_boxes = [n_boxes] * n_predictor_layers
            else:
                n_boxes = []
                for ar in aspect_ratios_per_layer:
                    if (1.0 in ar) and self.engine_configs.train_options.b_twoBoxesForAR1:
                        n_boxes.append(len(ar) + 1)
                    else:
                        n_boxes.append(len(ar))

            if self.engine_configs.train_options.s_scalingType == "global":
                self.engine_configs.train_options.l_scales.sort()
                min_scale = self.engine_configs.train_options.l_scales[0]
                max_scale = self.engine_configs.train_options.l_scales[1]
                scales = np.linspace(min_scale, max_scale, n_predictor_layers + 1)
            else:
                scales = self.engine_configs.train_options.l_scales

            if self.engine_configs.train_options.l_steps is None:
                steps = [None] * n_predictor_layers
            else:
                steps = self.engine_configs.train_options.l_steps

            if self.engine_configs.train_options.l_offsets is None:
                offsets = [None] * n_predictor_layers
            else:
                offsets = self.engine_configs.train_options.l_offsets

            class_layers = []
            box_layers = []
            anchor_layers = []
            class_layers_reshape = []
            box_layers_reshape = []
            anchor_layers_reshape = []
            predictor_sizes = []
            for j, hook in enumerate(hooks):
                if j == 0:
                    hook = L2Normalization()(hook)
                new_c_layer = keras.layers.Conv2D(n_boxes[j] * (self.engine_configs.train_options.i_numberOfBBDClasses + 1),
                                                  (3, 3), strides=(1, 1), padding='same',
                                                  kernel_initializer='he_normal')(hook)
                new_b_layer = keras.layers.Conv2D(n_boxes[j] * 4, (3, 3), strides=(1, 1), padding='same',
                                                  kernel_initializer='he_normal')(hook)
                new_a_layer = AnchorBoxes(self.engine_configs.layers.t_input_shape[0],
                                          self.engine_configs.layers.t_input_shape[1],
                                          this_scale=scales[j],
                                          next_scale=scales[j + 1],
                                          aspect_ratios=aspect_ratios[j],
                                          two_boxes_for_ar1=self.engine_configs.train_options.b_twoBoxesForAR1,
                                          this_steps=steps[j],
                                          this_offsets=offsets[j],
                                          clip_boxes=self.engine_configs.train_options.b_clipBoxes,
                                          variances=self.engine_configs.train_options.l_variances,
                                          coords=self.engine_configs.train_options.s_coordinatesType,
                                          normalize_coords=self.engine_configs.train_options.b_normalizeCoordinates)(new_b_layer)
                class_layers.append(new_c_layer)
                box_layers.append(new_b_layer)
                anchor_layers.append(new_a_layer)
                class_layers_reshape.append(keras.layers.Reshape((-1, (self.engine_configs.train_options.i_numberOfBBDClasses + 1)))(class_layers[-1]))
                box_layers_reshape.append(keras.layers.Reshape((-1, 4))(box_layers[-1]))
                anchor_layers_reshape.append(keras.layers.Reshape((-1, 8))(anchor_layers[-1]))
                predictor_sizes.append(class_layers[j]._keras_shape[1:3])

            classes_concat = keras.layers.Concatenate(axis=1)(class_layers_reshape)
            boxes_concat = keras.layers.Concatenate(axis=1)(box_layers_reshape)
            anchors_concat = keras.layers.Concatenate(axis=1)(anchor_layers_reshape)
            classes_softmax = keras.layers.Activation('softmax')(classes_concat)
            box_preds = keras.layers.Concatenate(axis=2)([classes_softmax, boxes_concat, anchors_concat])

            self.input_encoder = SSDInputEncoder(img_height=self.engine_configs.layers.t_input_shape[0],
                                                 img_width=self.engine_configs.layers.t_input_shape[1],
                                                 n_classes=self.engine_configs.train_options.i_numberOfBBDClasses,
                                                 predictor_sizes=predictor_sizes,
                                                 scales=scales,
                                                 aspect_ratios_global=global_aspect_ratios,
                                                 aspect_ratios_per_layer=aspect_ratios_per_layer,
                                                 two_boxes_for_ar1=self.engine_configs.train_options.b_twoBoxesForAR1,
                                                 steps=self.engine_configs.train_options.l_steps,
                                                 offsets=self.engine_configs.train_options.l_offsets,
                                                 clip_boxes=self.engine_configs.train_options.b_clipBoxes,
                                                 variances=self.engine_configs.train_options.l_variances,
                                                 pos_iou_threshold=self.engine_configs.train_options.f_posIouThreshold,
                                                 neg_iou_limit=self.engine_configs.train_options.f_negIouLimit,
                                                 normalize_coords=self.engine_configs.train_options.b_normalizeCoordinates)

            self.model = keras.models.Model(inputs=inputs, outputs=box_preds)

    def compile_graph(self):
        if self.engine_configs.train_options.i_nGpus > 1:
            self.parallel_model = ModelMGPU(self.model, self.engine_configs.train_options.i_nGpus)
            self.parallel_model.compile(optimizer=self.engine_configs.optimizer.optimizer,
                                        loss=self.engine_configs.loss_function.loss,
                                        metrics=self.engine_configs.monitors.monitors)
        else:
            self.model.compile(optimizer=self.engine_configs.optimizer.optimizer,
                               loss=self.engine_configs.loss_function.loss,
                               metrics=self.engine_configs.monitors.monitors)

    def train_graph(self):
        try:
            if self.engine_configs.val_data.valX is not None \
                    and self.engine_configs.train_options.f_validationSplit > 0.0:
                val_data = DataGenerator(images=self.engine_configs.val_data.valX,
                                         labels=self.engine_configs.val_data.valY)

                val_data_size = val_data.get_dataset_size()
                val_steps = np.ceil(val_data_size / self.engine_configs.train_options.i_batchSize)

                val_generator = val_data.generate(batch_size=self.engine_configs.train_options.i_batchSize,
                                                  shuffle=False,
                                                  transformations=[],
                                                  label_encoder=self.input_encoder,
                                                  returns={'processed_images',
                                                           'encoded_labels'},
                                                  keep_images_without_gt=False)
            elif self.engine_configs.val_data.valX is None and self.engine_configs.train_options.f_validationSplit > 0:
                val_split = self.engine_configs.train_options.f_validationSplit
                n_tot_imgs = len(self.engine_configs.train_data.trainX)
                n_val_imgs = floor(val_split * n_tot_imgs)
                self.engine_configs.val_data.valX = self.engine_configs.train_data.trainX[:n_val_imgs]
                self.engine_configs.val_data.valY = self.engine_configs.train_data.trainY[:n_val_imgs]
                self.engine_configs.train_data.trainX = self.engine_configs.train_data.trainX[n_val_imgs:]
                self.engine_configs.train_data.trainY = self.engine_configs.train_data.trainY[n_val_imgs:]

                val_data = DataGenerator(images=self.engine_configs.val_data.valX,
                                         labels=self.engine_configs.val_data.valY)

                val_data_size = val_data.get_dataset_size()
                val_steps = np.ceil(val_data_size / self.engine_configs.train_options.i_batchSize)

                val_generator = val_data.generate(batch_size=self.engine_configs.train_options.i_batchSize,
                                                  shuffle=False,
                                                  transformations=[],
                                                  label_encoder=self.input_encoder,
                                                  returns={'processed_images',
                                                           'encoded_labels'},
                                                  keep_images_without_gt=False)
            else:
                # No validation data
                val_steps = None
                val_generator = None

        except:
            self.errors.append('Level3Error:CouldNotDetermineBbdValidationData')

        data_augmentation_chain = DataAugmentationConstantInputSize(random_brightness=(-48, 48, 0.5),
                                                                    random_contrast=(0.5, 1.8, 0.5),
                                                                    random_saturation=(0.5, 1.8, 0.5),
                                                                    random_hue=(18, 0.5),
                                                                    random_flip=0.5,
                                                                    random_translate=((0.03, 0.5), (0.03, 0.5), 0.5),
                                                                    random_scale=(0.5, 2.0, 0.5),
                                                                    n_trials_max=3,
                                                                    clip_boxes=True,
                                                                    overlap_criterion='area',
                                                                    bounds_box_filter=(0.3, 1.0),
                                                                    bounds_validator=(0.5, 1.0),
                                                                    n_boxes_min=1,
                                                                    background=(0, 0, 0),
                                                                    data_min=float(self.engine_configs.data_preprocessing.f_minImageIntensity),
                                                                    data_max=float(self.engine_configs.data_preprocessing.f_maxImageIntensity))
        train_data = DataGenerator(images=self.engine_configs.train_data.trainX,
                                   labels=self.engine_configs.train_data.trainY)
        train_data_size = train_data.get_dataset_size()
        steps_per_epoch = np.ceil(train_data_size / self.engine_configs.train_options.i_batchSize)
        final_epoch = self.engine_configs.train_options.i_epochs

        train_generator = train_data.generate(batch_size=self.engine_configs.train_options.i_batchSize,
                                              shuffle=True,
                                              transformations=[data_augmentation_chain],
                                              label_encoder=self.input_encoder,
                                              returns={'processed_images',
                                                       'encoded_labels'},
                                              keep_images_without_gt=False)

        with self.graph.as_default():
            if self.engine_configs.train_options.i_nGpus > 1:
                self.parallel_model.fit_generator(generator=train_generator,
                                                  steps_per_epoch=steps_per_epoch,
                                                  epochs=final_epoch,
                                                  callbacks=self.engine_configs.callbacks.callbacks,
                                                  validation_data=val_generator,
                                                  validation_steps=val_steps,
                                                  initial_epoch=0)
            else:
                self.model.fit_generator(generator=train_generator,
                                         steps_per_epoch=steps_per_epoch,
                                         epochs=final_epoch,
                                         callbacks=self.engine_configs.callbacks.callbacks,
                                         validation_data=val_generator,
                                         validation_steps=val_steps,
                                         initial_epoch=0)
        pass

    def retrain_graph(self):
        pass

    def predict_on_graph(self):
        try:
            self.model = keras.models.load_model(self.engine_configs.loader.s_loadModelPath)
            predictions = self.model.predict(self.engine_configs.test_data.testX)
            predictions = decode_detections(predictions,
                                            confidence_thresh=self.engine_configs.train_options.f_confidenceThreshold,
                                            iou_threshold=self.engine_configs.train_options.f_iouThreshold,
                                            top_k=self.engine_configs.train_options.i_topK,
                                            normalize_coords=self.engine_configs.train_options.b_normalizeCoordinates,
                                            img_height=self.engine_configs.layers.t_input_shape[0],
                                            img_width=self.engine_configs.layers.t_input_shape[1])

            stamp = datetime.datetime.fromtimestamp(time.time()).strftime('date_%Y_%m_%d_time_%H_%M_%S')

            write_hdf5('bbd_predictions_' + stamp + '.h5', predictions)
        except:
            self.errors.append('Level3Error:CouldNotMakePredictionsonBbdGraph')