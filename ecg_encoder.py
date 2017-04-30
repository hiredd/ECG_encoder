import os
import time
import math
import itertools as it

import tensorflow as tf
from tensorflow.python.framework import ops
import numpy as np
from tqdm import tqdm

import ecg_encoder_tools as utils


def simple_decoder_fn_train_(encoder_state, name=None):

    with ops.name_scope(name, "simple_decoder_fn_train", [encoder_state]):
        pass

    def decoder_fn(time, cell_state, cell_input, cell_output, context_state):
        """ Decoder function used in the `dynamic_rnn_decoder` with the purpose of
        training.
        Args:
          time: positive integer constant reflecting the current timestep.
          cell_state: state of RNNCell.
          cell_input: input provided by `dynamic_rnn_decoder`.
          cell_output: output of RNNCell.
          context_state: context state provided by `dynamic_rnn_decoder`.
        Returns:
          A tuple (done, next state, next input, emit output, next context state)
            where:
          done: `None`, which is used by the `dynamic_rnn_decoder` to indicate
            that `sequence_lengths` in `dynamic_rnn_decoder` should be used.
          next state: `cell_state`, this decoder function does not modify the
            given state.
          next input: `cell_input`, this decoder function does not modify the
            given input. The input could be modified when applying e.g. attention.
          emit output: `cell_output`, this decoder function does not modify the
          given output.
          next context state: `context_state`, this decoder function does not
          modify the given context state. The context state could be modified when
          applying e.g. beam search.
        """
        with ops.name_scope(name, "simple_decoder_fn_train",
                            [time, cell_state, cell_input, cell_output,
                             context_state]):
            if cell_state is None:  # first call, return encoder_state
                return (None, encoder_state, tf.zeros_like(encoder_state), cell_output,
                    context_state)
            else:
                return (None, cell_state, cell_output, cell_output, context_state)
    return decoder_fn

class ECGEncoder(object):

    def __init__(self, n_frames, n_channel, n_hidden_RNN, reduction_ratio,
        use_true_inps, do_train):

        self.n_frames = n_frames
        self.n_channel = n_channel
        self.n_hidden_RNN = n_hidden_RNN
        self.reduction_ratio = reduction_ratio
        self.use_true_inps = use_true_inps
        self.create_graph()
        if do_train: self.create_optimizer_graph(self.cost)
        os.makedirs('summary', exist_ok=True)
        sub_d = len(os.listdir('summary'))
        self.train_writer = tf.summary.FileWriter(logdir = 'summary/'+str(sub_d))
        self.merged = tf.summary.merge_all()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self.sess = tf.Session(config = config)
        self.sess.run(tf.global_variables_initializer())

        self.saver = tf.train.Saver(var_list=tf.global_variables(),
                                    max_to_keep = 1000)
        


    # --------------------------------------------------------------------------
    def __enter__(self):
        return self


    # --------------------------------------------------------------------------
    def __exit__(self, exc_type, exc_val, exc_tb):
        tf.reset_default_graph()
        if self.sess is not None:
            self.sess.close()
        

    # --------------------------------------------------------------------------
    def create_graph(self):
        print('Creat graph')
        self.inputs,\
        self.sequence_length,\
        self.keep_prob,\
        self.weight_decay,\
        self.learn_rate = self.input_graph()

        # Encoder
        convo = self.convo_graph(self.inputs) #b*n_f x h1 x c1
        # print('convo', convo)

        seq_l = tf.cast((self.sequence_length/self.reduction_ratio), tf.int32)
        frame_embs = self.compress_frames_RNN(convo, seq_l, n_layers=2)
        frame_embs = tf.reshape(frame_embs, [-1, self.n_frames, self.n_hidden_RNN])
        # b x n_f x hRNN
        # print('frame_embs', frame_embs)# b x n_f x hRNN

        self.Z = self.encode_to_Z(inputs=frame_embs, n_layers=2) #b x hRNN
        # print('Z', self.Z)



        # Decoder
        r_frame_embs = self.decode_from_Z(encoded_state=self.Z,
            inputs=frame_embs, n_layers=1) # b x n_f x hRNN
        # print('r_frame_embs', r_frame_embs)

        r_frame_embs = tf.reshape(r_frame_embs, [-1, self.n_hidden_RNN])# b*n_f x hRNN
        r_convo = self.decompress_frames_RNN(encoded_state=r_frame_embs,
            seq_lengths=seq_l, inputs=convo, n_layers=1) #b*n_f x h1 x c1
        # print('r_convo', r_convo)

        self.r_inputs = self.deconvo_graph(r_convo) #b*n_f x h2 x c2
        # print('r_inputs', self.r_inputs)



        self.cost = self.create_cost_graph(original=self.inputs,
            recovered=self.r_inputs, Z=self.Z)
        print('Done!')


    # --------------------------------------------------------------------------
    def input_graph(self):
        print('\tinput_graph')
        inputs = tf.placeholder(tf.float32,
            shape=[None, None, self.n_channel],
            name='inputs') #b*n_f x h x c (h is variable value)

        sequence_length = tf.placeholder(tf.int32, shape=[None],
            name='sequence_length') # b*n_f

        keep_prob = tf.placeholder(tf.float32, name='keep_prob')

        weight_decay = tf.placeholder(tf.float32, name='weight_decay')

        learn_rate = tf.placeholder(tf.float32, name='learn_rate')

        return inputs, sequence_length, keep_prob, weight_decay, learn_rate


    # --------------------------------------------------------------------------
    def convo_graph(self, inputs):
        print('\tconvo_graph')
        with tf.variable_scope('convo_graph'):
            convo1 = self.conv_1d(inputs,
                kernelShape=[2, 3, 16],
                strides=2,
                activation=tf.nn.elu,
                keep_prob=self.keep_prob)
            convo2 = self.conv_1d(convo1,
                kernelShape=[2, 16, 32],
                strides=2,
                activation=tf.nn.elu,
                keep_prob=self.keep_prob)
            convo3 = self.conv_1d(convo2,
                kernelShape=[2, 32, 64],
                strides=2,
                activation=tf.nn.elu,
                keep_prob=self.keep_prob)
        return convo3


    # --------------------------------------------------------------------------
    def conv_1d(self, inputs, kernelShape, strides=1, activation = None, keep_prob = None):
        print('\t\tconv_1d')
        with tf.variable_scope(None, 'conv_1d') as sc:
            # inputs to convo need [batch, in_width, in_channels]
            # kernels shape must be [filter_width, in_channels, out_channels]
            kernel = tf.get_variable('kernel', shape=kernelShape,
                initializer=tf.contrib.layers.xavier_initializer())
            bias = tf.get_variable('bias', shape=[kernelShape[2]],
                initializer=tf.constant_initializer(0.0))
            convo = tf.nn.bias_add(tf.nn.conv1d(inputs, filters=kernel,
                stride=strides, padding="SAME"), bias)
            if activation is not None:
                convo = activation(convo, name='activation')
            if keep_prob is not None:
                convo = tf.nn.dropout(convo, keep_prob=keep_prob, name="keep_prob")
            return convo


    # --------------------------------------------------------------------------
    def compress_frames_RNN(self, inputs, sequence_length, n_layers):
        print('\tcompress_frames_RNN')
        with tf.variable_scope('compress_frames_RNN'):
            # inputs b*n_f x h x c (h is variable value)
            # sequence_length b*n_f
            cell = tf.contrib.rnn.GRUCell(self.n_hidden_RNN)

            fw_cell = tf.contrib.rnn.MultiRNNCell([cell]*n_layers)
            fw_cell = tf.contrib.rnn.DropoutWrapper(fw_cell,
                output_keep_prob=self.keep_prob)

            bw_cell = tf.contrib.rnn.MultiRNNCell([cell]*n_layers)
            bw_cell = tf.contrib.rnn.DropoutWrapper(bw_cell,
                output_keep_prob=self.keep_prob)

            outputs, states = tf.nn.bidirectional_dynamic_rnn(cell_fw=fw_cell,
                cell_bw=bw_cell,
                inputs=inputs,
                sequence_length=sequence_length,
                dtype=tf.float32)
        return states[0][-1] + states[1][-1] #shape b*n_f x hRNN


    # --------------------------------------------------------------------------
    def encode_to_Z(self, inputs, n_layers):
        print('\tencode_to_Z')
        with tf.variable_scope('encode_to_Z'):
            # inputs b*(n_b+o) x h x c (h is variable value)
            # sequence_length b*(n_b+o)
            cell = tf.contrib.rnn.GRUCell(self.n_hidden_RNN)

            fw_cell = tf.contrib.rnn.MultiRNNCell([cell]*n_layers)
            fw_cell = tf.contrib.rnn.DropoutWrapper(fw_cell,
                output_keep_prob=self.keep_prob)

            bw_cell = tf.contrib.rnn.MultiRNNCell([cell]*n_layers)
            bw_cell = tf.contrib.rnn.DropoutWrapper(bw_cell,
                output_keep_prob=self.keep_prob)

            sl = tf.tile([self.n_frames], [tf.shape(inputs)[0]])
            outputs, states = tf.nn.bidirectional_dynamic_rnn(cell_fw=fw_cell,
                cell_bw=bw_cell,
                inputs=inputs,
                sequence_length=sl,
                dtype=tf.float32)
        return states[0][-1] + states[1][-1] #shape b*n_f x hRNN


    # --------------------------------------------------------------------------
    def decode_from_Z(self, encoded_state, inputs, n_layers):
        print('\tdecode_from_Z')
        # first step is zero-vectors
        inputs = tf.concat([tf.zeros_like(inputs[:,0:1,:]), inputs], axis=1)[:,:-1,:]

        with tf.variable_scope('decode_from_Z'):
            decoder_fn = tf.contrib.seq2seq.simple_decoder_fn_train(encoded_state)\
                if self.use_true_inps else simple_decoder_fn_train_(encoded_state)
            dec_cell = tf.contrib.rnn.GRUCell(self.n_hidden_RNN)
            # dec_cell = tf.contrib.rnn.MultiRNNCell([dec_cell]*n_layers)
            # dec_cell = tf.contrib.rnn.DropoutWrapper(dec_cell,
                # output_keep_prob=self.keep_prob)

            sl = tf.tile([self.n_frames], [tf.shape(inputs)[0]])
            recover, _, _ = tf.contrib.seq2seq.dynamic_rnn_decoder(
                cell=dec_cell,
                decoder_fn=decoder_fn,
                inputs=inputs if self.use_true_inps else None,
                sequence_length=sl)

            return recover


    # --------------------------------------------------------------------------
    def decompress_frames_RNN(self, encoded_state, inputs, seq_lengths, n_layers):
        print('\tdecompress_frames_RNN')
        # first step is zero-vectors
        inputs = tf.concat([tf.zeros_like(inputs[:,0:1,:]), inputs], axis=1)[:,:-1,:]

        with tf.variable_scope('decompress_frames_RNN'):
            decoder_fn = tf.contrib.seq2seq.simple_decoder_fn_train(encoded_state)\
                if self.use_true_inps else simple_decoder_fn_train_(encoded_state)
            dec_cell = tf.contrib.rnn.GRUCell(self.n_hidden_RNN)
            # dec_cell = tf.contrib.rnn.MultiRNNCell([dec_cell]*n_layers)
            # dec_cell = tf.contrib.rnn.DropoutWrapper(dec_cell,
                # output_keep_prob=self.keep_prob)

            recover, _, _ = tf.contrib.seq2seq.dynamic_rnn_decoder(
                cell=dec_cell,
                decoder_fn=decoder_fn,
                inputs=inputs if self.use_true_inps else None,
                sequence_length=seq_lengths)

            return recover


    # --------------------------------------------------------------------------
    def deconvo_graph(self, inputs):
        # inputs [b, h, c]
        print('\tdeconvo_graph')
        deconvo1 = self.deconv_1d(inputs=inputs, filters=32, kernel_size=2, strides=2)
        deconvo2 = self.deconv_1d(inputs=deconvo1, filters=16, kernel_size=2, strides=2)
        deconvo3 = self.deconv_1d(inputs=deconvo2, filters=3, kernel_size=2, strides=2)
        return deconvo3


    # --------------------------------------------------------------------------
    def deconv_1d(self, inputs, filters, kernel_size, strides, activation = None,
        keep_prob = None):
        """
        Args:
            inputs: tensor of shape [batch, width, in_channels]
            filters: integer, the dimensionality of the output space
            (i.e. the number output of filters in the convolution).
            kernel_size: integer specifying the spatial dimensions of of the filters
            strides: integer specifying the strides of the convolution.
        """
        print('\t\tdeconv_1d')
        inputs = tf.expand_dims(inputs, 2)
        with tf.variable_scope(None, 'deconv_1d') as sc:
            convo = tf.layers.conv2d_transpose(inputs=inputs,
                filters=filters,
                kernel_size=[kernel_size,1],
                strides=[strides, 1],
                padding='same',
                activation=activation,
                kernel_initializer=tf.random_normal_initializer(mean=0.0, stddev=0.01))
            convo = tf.squeeze(convo, axis=2)
            if keep_prob is not None:
                convo = tf.nn.dropout(convo, keep_prob=keep_prob, name="keep_prob")
            return convo


    # --------------------------------------------------------------------------
    def create_cost_graph(self, original, recovered, Z):
        print('\tcreate_cost_graph')

        self.mse = tf.reduce_mean(tf.square(original - recovered))

        self.L2_loss = self.weight_decay*sum([tf.reduce_mean(tf.square(var))
            for var in tf.trainable_variables()])

        self.Z_L2_loss = tf.reduce_mean(tf.square(Z))

        tf.summary.scalar('MSE', self.mse)
        tf.summary.scalar('L2 loss', self.L2_loss)
        tf.summary.scalar('Z L2 loss', self.Z_L2_loss)
        return self.mse + self.L2_loss + self.Z_L2_loss


    # --------------------------------------------------------------------------
    def create_optimizer_graph(self, cost):
        print('create_optimizer_graph')
        with tf.variable_scope('optimizer_graph'):
            optimizer = tf.train.AdamOptimizer(self.learn_rate)
            self.train = optimizer.minimize(cost)

        
    #---------------------------------------------------------------------------  
    def save_model(self, path = 'beat_detector_model', step = None):
        p = self.saver.save(self.sess, path, global_step = step)
        print("\tModel saved in file: %s" % p)

    #---------------------------------------------------------------------------
    def load_model(self, path):
        #path is path to file or path to directory
        #if path it is path to directory will be load latest model
        load_path = os.path.splitext(path)[0]\
        if os.path.isfile(path) else tf.train.latest_checkpoint(path)
        print('try to load {}'.format(load_path))
        self.saver.restore(self.sess, load_path)
        print("Model restored from file %s" % load_path)


    #---------------------------------------------------------------------------
    def train_(self, data_loader,  keep_prob, weight_decay, learn_rate_start,
        learn_rate_end, n_iter, save_model_every_n_iter, path_to_model):
        print('\n\n\n\t----==== Training ====----')
        #try to load model
        try:
            self.load_model(os.path.dirname(path_to_model))
        except:
            print('Can not load model {0}, starting new train'.format(path_to_model))
            
        start_time = time.time()
        b = math.log(learn_rate_start/learn_rate_end, n_iter) 
        a = learn_rate_start*math.pow(1, b)
        for current_iter in tqdm(range(n_iter)):
            learn_rate = a/math.pow((current_iter+1), b)
            batch = data_loader.get_batch()
            feedDict = {self.inputs : batch['normal_data'],
                        self.sequence_length : batch['sequence_length'],
                        self.keep_prob : keep_prob,
                        self.weight_decay : weight_decay,
                        self.learn_rate : learn_rate}
            _, summary = self.sess.run([self.train, self.merged], feed_dict = feedDict)
            self.train_writer.add_summary(summary, current_iter)

            if (current_iter+1) % save_model_every_n_iter == 0:
                self.save_model(path = path_to_model, step = current_iter+1)

        self.save_model(path = path_to_model, step = current_iter+1)
        print('\nTrain finished!')
        print("Training time --- %s seconds ---" % (time.time() - start_time))


    #############################################################################################################
    def predict(self, path_to_file, path_to_save, path_to_model, use_delta_coding):


        predicting_time = time.time()
        print('\n\n\n\t----==== Predicting beats ====----')
        #load model
        self.load_model(path_to_model)
        
        data = np.load(path_to_file).item()

        gen = utils.step_generator(data,
                   n_frames = self.n_frames,
                   overlap = 0,
                   get_data = not use_delta_coding,
                   get_delta_coded_data = use_delta_coding,
                   rr = self.reduction_ratio,
                   get_events = False)
        
        result = np.empty([0,self.n_channel])

        forward_pass_time = 0
        for current_iter in tqdm(it.count()):
            try:
                batch = next(gen)
            except StopIteration:
                break
            feedDict = {self.inputs : batch['normal_data'],
                        self.sequence_length : batch['sequence_length'],
                        self.keep_prob : 1}
            start_time = time.time()
            res = self.sess.run(self.r_inputs, feed_dict = feedDict) #n_f x h x c
            forward_pass_time = forward_pass_time + (time.time() - start_time)
            for f in range(self.n_frames):
                h = batch['seq_l']
                a = np.reshape(res[f,:h[f], :], [-1, self.n_channel])
                result = np.concatenate((result, a), 0)


            
        if path_to_save is not None:
            np.save(path_to_save, result)
            print('\nfile saved ', path_to_save)

        print('forward_pass_time = ', forward_pass_time)
        print('predicting_time = ', time.time() - predicting_time)

# testing #####################################################################################################################
if __name__ == '__main__':
    ECGEncoder(n_frames=10,
        n_channel=3,
        n_hidden_RNN=128,
        reduction_ratio=8,
        do_train=True)
