import tensorflow as tf
import tensorflow_addons as tfa
from tensorflow.python.keras.layers import Dense, Dropout,  concatenate, Flatten, LSTMCell, GRUCell, Embedding

from tensorflow.keras.optimizers import RMSprop
from utils import preprocess_image
import numpy as np
from constants import START_WORD, PLACEHOLDER, IMAGE_SIZE
from models.shallow_cnn_model import ShallowCnnUnit, CounterUnit
from models.training_inference_sampler import TrainingInferenceSampler
from tensorflow.python.keras.layers.recurrent import _generate_zero_filled_state
from tensorflow.python.ops import array_ops



class CounterUnitAfterOrdering(tf.keras.layers.Layer):
    def __init__(self, layer_size, dropout_ratio, activation='relu', **kwargs):
        super().__init__(**kwargs)
        self.main_layers = [Dense(layer_size, activation=activation, name="counter1"),
                       Dense(layer_size, activation=activation, name="counter2")
                            ]
        self.last_layer = Dense(1, activation=activation, name='counter_out')

    def call(self, inputs):
        inp = inputs
        for layer in self.main_layers:
            inp = layer(inp)
        return self.last_layer(inp)


class RnnImageModelTfaRepeat(tf.keras.models.Model):

    def __init__(self, words, image_count_words, *args, max_code_length, activation='relu', cell_type='lstm',
                 kernel_shape=7, dropout_ratio=0.25, dense_layer_size=512, image_out=False,
                 order_layer_output_size=1024, embedding_size=None, **kwargs):

        super().__init__(*args, **kwargs)
        self.image_out = image_out
        self.voc_size = len(words)
        self.image_out = image_out
        self.layer_output_names = words
        self.image_count_words = image_count_words
        self.max_code_length = max_code_length
        self.cell_type = cell_type
        self.order_layer_output_size = order_layer_output_size
        self.embedding_size = embedding_size

        self.shallow_cnn_unit = ShallowCnnUnit(image_count_words=image_count_words, kernel_shape=kernel_shape,
                                               dropout_ratio=dropout_ratio, activation=activation, name='cnn_unit')

        self.flatten_img = Flatten(name='ordering_flatten')
        self.ordering_1 = Dense(1024, activation=activation, name='ordering_1')
        self.ordering_1_drop = Dropout(dropout_ratio, name='ordering_drop_1')
        self.ordering_2 = Dense(1024, activation=activation, name='ordering_2')
        self.ordering_2_drop = Dropout(dropout_ratio, name='ordering_drop_2')
        self.memory_prep = Dense(order_layer_output_size, activation=activation, name='memory_prep')

        self.object_count_layers = [CounterUnitAfterOrdering(dense_layer_size,dropout_ratio=dropout_ratio, name=word)
                                    for word in self.image_count_words]

        if cell_type == 'lstm':
            self.decoder_cell = LSTMCell(order_layer_output_size, name='lstm_cell', recurrent_dropout=dropout_ratio)
        elif cell_type == 'gru':
            self.decoder_cell = GRUCell(order_layer_output_size, name='gru_cell', recurrent_dropout=dropout_ratio)
        else:
            raise ValueError("Unknown cell_type {}".format(cell_type))

        self.output_layer = Dense(len(words), activation='softmax', name='code_out')

        if embedding_size is not None:
            self.embedder = Embedding(self.voc_size, embedding_size, name='embedder')
        else:
            self.embedder = None

    def call(self, inputs, training=None, mask=None):
        inp = inputs['img_data']
        context_raw = inputs['context']
        self.decoder_cell.reset_recurrent_dropout_mask()

        # Handle embedding in case it is used:
        if self.embedding_size is None:
            context_inp = context_raw
        else:
            context_inp = self.embedder(context_raw)

        # First handle the image part
        cnn_output_layers = self.shallow_cnn_unit(inp)
        cnn_conc = concatenate(cnn_output_layers)
        cnn_out = self.flatten_img(cnn_conc)

        order_2_inp = self.ordering_1_drop(self.ordering_1(cnn_out))
        order_2_out = self.ordering_2_drop(self.ordering_2(order_2_inp))


        obj_counter_outputs = {key + "_count": layer(order_2_out)
                               for key, layer in zip(self.image_count_words, self.object_count_layers)}

        memory_in = self.memory_prep(order_2_out)

        # Then handle the rnn
        batch_size = array_ops.shape(context_inp)[0]
        self.decoder_cell.reset_recurrent_dropout_mask()
        if self.cell_type == 'lstm':
            init_state_h = _generate_zero_filled_state(batch_size, self.order_layer_output_size, tf.float32)
            encoder_state = [init_state_h, memory_in]
        elif self.cell_type == 'gru':
            encoder_state = [memory_in]

        sampler = TrainingInferenceSampler(training=training, voc_size=self.voc_size,
                                           max_sequence_length=self.max_code_length, embedder=self.embedder,
                                           img_input=order_2_out)

        decoder = tfa.seq2seq.basic_decoder.BasicDecoder(self.decoder_cell, sampler,
                                                         output_layer=self.output_layer)

        final_outputs, final_state, final_sequence_lengths = decoder(
            context_inp, initial_state=encoder_state, training=training)

        final_outputs = final_outputs.rnn_output
        outputs = obj_counter_outputs
        outputs.update({'code': final_outputs})

        if self.image_out:
            outputs.update({"img_out_" + key: layer for key, layer in zip(self.image_count_words,
                                                                          cnn_output_layers)})
        return outputs

    def compile(self, loss='categorical_crossentropy', optimizer=RMSprop(lr=0.0001, clipvalue=1.0), **kwargs):
        # Fix the bug in tensorflow 1.15 that sets the outputnames wrong when using dicts and generators
        if self.image_out:
            names = ([key + "_count" for key in self.image_count_words]
                     + ["img_out_" + key for key in self.image_count_words]
                     + ["code"])
        else:
            names = [key + "_count" for key in self.image_count_words] + ['code']
        self.output_names = sorted(names)

        return super().compile(loss=loss, optimizer=optimizer, **kwargs)

    def predict(self, *args, return_as_dict=True, **kwargs):
        pred = super().predict(*args, **kwargs)
        if return_as_dict:
            return {key: val for key, val in zip(self.output_names, pred)}
        else:
            return pred

    def predict_image(self, image, voc, img_size=IMAGE_SIZE):
        if isinstance(image, str):
            img = preprocess_image(image, img_size)
        elif isinstance(image, np.ndarray):
            img = image
        else:
            raise TypeError("Unknown handling of image input of type {}".format(type(image)))

        img_inp = np.expand_dims(img, 0)

        current_context = np.array([[voc.word2one_hot_dict[PLACEHOLDER]]])
        current_context[0, 0, :] = voc.word2one_hot_dict[START_WORD]

        probas = self.predict({'img_data': img_inp, 'context': current_context})['code'][0]
        return " ".join([voc.token2word_dict[val] for val in np.argmax(probas, axis=1)]).split(" <eos>")[0].replace(" <pad>", "")