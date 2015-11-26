"""
    Sequence-to-sequence model with the attention mechanism described in


    and support to buckets

"""

import random
import numpy as np
import tensorflow as tf
from tensorflow.models.rnn import rnn_cell, seq2seq
import data_utils
import nmt


class NMTModel(object):
    """Neural Machine Translation model with attention and for multiple buckets.

    This class implements single-layer or multi-layer bi-directional recurrent neural
    network as encoder, and an attention-based decoder. This is the same as the model
    described in this paper: arxiv.org/abs/1412.2007 - please look there for details,
    or into the seq2seq library for some function implementations.

    This class also allows to use GRU cells in addition to LSTM cells, and
    sampled softmax to handle large output vocabulary size.

    A single-layer version of this model, but with bi-directional encoder, was presented in
      http://arxiv.org/abs/1409.0473

    """

    def __init__(self,
                 source_vocab_size,
                 target_vocab_size,
                 buckets,
                 source_proj_size,
                 target_proj_size,
                 encoder_size,
                 decoder_size,
                 num_layers,
                 max_gradient_norm,
                 batch_size,
                 learning_rate,
                 learning_rate_decay_factor,
                 optimizer='sgd',
                 use_lstm=False,
                 num_samples=512,
                 forward_only=False,
                 dtype=tf.float32):
        """Create the model.

        Args:
          source_vocab_size: size of the source vocabulary.
          target_vocab_size: size of the target vocabulary.
          buckets: a list of pairs (I, O), where I specifies maximum input length
            that will be processed in that bucket, and O specifies maximum output
            length. Training instances that have inputs longer than I or outputs
            longer than O will be pushed to the next bucket and padded accordingly.
            We assume that the list is sorted, e.g., [(2, 4), (8, 16)].
          encoder_size: number of units in each layer of the model.
          num_layers: number of layers in the model.
          max_gradient_norm: gradients will be clipped to maximally this norm.
          batch_size: the size of the batches used during training;
            the model construction is independent of batch_size, so it can be
            changed after initialization if this is convenient, e.g., for decoding.
          learning_rate: learning rate to start with.
          learning_rate_decay_factor: decay learning rate by this much when needed.
          use_lstm: if true, we use LSTM cells instead of GRU cells.
          num_samples: number of samples for sampled softmax.
          forward_only: if set, we do not construct the backward pass in the model.
        """
        self.source_vocab_size = source_vocab_size
        self.target_vocab_size = target_vocab_size
        self.buckets = buckets
        self.batch_size = batch_size
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
        self.learning_rate_decay_op = self.learning_rate.assign(
            self.learning_rate * learning_rate_decay_factor)
        self.global_step = tf.Variable(0, trainable=False)
        self.source_proj_size = source_proj_size
        self.target_proj_size = target_proj_size
        self.encoder_size = encoder_size
        self.decoder_size = decoder_size

        self.dtype = dtype

        # If we use sampled softmax, we need an output projection.
        self.output_projection = None
        softmax_loss_function = None
        # Sampled softmax only makes sense if we sample less than vocabulary size.
        if 0 < num_samples < self.target_vocab_size:
            with tf.device("/cpu:0"):
                w = tf.get_variable("proj_w", [encoder_size, self.target_vocab_size])
                w_t = tf.transpose(w)
                b = tf.get_variable("proj_b", [self.target_vocab_size])
            self.output_projection = (w, b)

            def sampled_loss(inputs, labels):
                with tf.device("/cpu:0"):
                    labels = tf.reshape(labels, [-1, 1])
                    return tf.nn.sampled_softmax_loss(w_t, b, inputs, labels, num_samples,
                                                      self.target_vocab_size)

            softmax_loss_function = sampled_loss

        # Create the internal multi-layer cell for our RNN.
        self.encoder_cell = nmt.GRU(self.source_proj_size, encoder_size)
        self.encoder_cell_r = nmt.GRU(self.source_proj_size, encoder_size)
        self.decoder_cell = nmt.GRU(self.target_proj_size, decoder_size)
        if use_lstm:
            self.encoder_cell = rnn_cell.LSTMCell(encoder_size, input_size=self.source_proj_size)
            self.encoder_cell_r = rnn_cell.LSTMCell(encoder_size, input_size=self.source_proj_size)
            self.decoder_cell = rnn_cell.LSTMCell(decoder_size, input_size=self.target_proj_size)

        # The seq2seq function: we use embedding for the input and attention.
        def seq2seq_f(encoder_inputs, encoder_inputs_r, decoder_inputs, do_decode):
            return self.inference(encoder_inputs, encoder_inputs_r, decoder_inputs, do_decode)

        # Feeds for inputs.
        self.encoder_inputs = []
        self.encoder_inputs_r = []
        self.decoder_inputs = []
        self.target_weights = []

        for i in xrange(buckets[-1][0]):  # Last bucket is the biggest one.
            self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="encoder{0}".format(i)))

            self.encoder_inputs_r.append(tf.placeholder(tf.int32, shape=[None],
                                                        name="encoder_r{0}".format(i)))

        for i in xrange(buckets[-1][1] + 1):
            self.decoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="decoder{0}".format(i)))

            self.target_weights.append(tf.placeholder(tf.float32, shape=[None],
                                                      name="weight{0}".format(i)))

        # Our targets are decoder inputs shifted by one.
        targets = [self.decoder_inputs[i + 1]
                   for i in xrange(len(self.decoder_inputs) - 1)]

        # Training outputs and losses.
        if forward_only:
            self.outputs, self.losses = nmt.bidirectional_model_with_buckets(
                self.encoder_inputs, self.encoder_inputs_r, self.decoder_inputs,
                targets, self.target_weights, buckets, self.target_vocab_size,
                lambda x, xr, y: seq2seq_f(x, xr, y, True),
                softmax_loss_function=softmax_loss_function)
            # If we use output projection, we need to project outputs for decoding.
            if self.output_projection is not None:
                for b in xrange(len(buckets)):
                    self.outputs[b] = [tf.nn.xw_plus_b(output, self.output_projection[0],
                                                       self.output_projection[1])
                                       for output in self.outputs[b]]
        else:
            self.outputs, self.losses = nmt.bidirectional_model_with_buckets(
                self.encoder_inputs, self.encoder_inputs_r, self.decoder_inputs,
                targets, self.target_weights, buckets, self.target_vocab_size,
                lambda x, xr, y: seq2seq_f(x, xr, y, False),
                softmax_loss_function=softmax_loss_function)

        # Gradients and SGD update operation for training the model.
        params = tf.trainable_variables()
        if not forward_only:
            self.gradient_norms = []
            self.updates = []
            # opt = tf.train.GradientDescentOptimizer(self.learning_rate)
            opt = self._get_optimizer(optimizer, learning_rate)
            for b in xrange(len(buckets)):
                gradients = tf.gradients(self.losses[b], params)
                clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                                 max_gradient_norm)
                self.gradient_norms.append(norm)
                self.updates.append(opt.apply_gradients(
                    zip(clipped_gradients, params), global_step=self.global_step))

        self.saver = tf.train.Saver(tf.all_variables())

    def _get_optimizer(self, name='sgd', lr_rate=0.1, decay=1e-4):
        """

        Parameters
        ----------
        name
        lr_rate
        decay

        Returns
        -------

        """
        optimizer = None
        if name is 'sgd':
            optimizer = tf.train.GradientDescentOptimizer(lr_rate)
        elif name is 'adagrad':
            optimizer = tf.train.AdagradOptimizer(lr_rate)
        elif name is 'adam':
            optimizer = tf.train.AdamOptimizer(lr_rate)
        elif name is 'rmsprop':
            optimizer = tf.train.RMSPropOptimizer(lr_rate, decay)
        else:
            raise ValueError('Optimizer not found.')
        return optimizer

    def step(self, session, encoder_inputs, encoder_inputs_r, decoder_inputs, target_weights,
             bucket_id, forward_only):
        """Run a step of the model feeding the given inputs.

        Args:
          session: tensorflow session to use.
          encoder_inputs: list of numpy int vectors to feed as encoder inputs.
          encoder_inputs_r: list of numpy int vectors to feed as encoder inputs (reversed sentence).
          decoder_inputs: list of numpy int vectors to feed as decoder inputs.
          target_weights: list of numpy float vectors to feed as target weights.
          bucket_id: which bucket of the model to use.
          forward_only: whether to do the backward step or only forward.

        Returns:
          A triple consisting of gradient norm (or None if we did not do backward),
          average perplexity, and the outputs.

        Raises:
          ValueError: if length of enconder_inputs, decoder_inputs, or
            target_weights disagrees with bucket size for the specified bucket_id.
        """
        # Check if the sizes match.
        encoder_size, decoder_size = self.buckets[bucket_id]
        if len(encoder_inputs) != encoder_size:
            raise ValueError("Encoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(encoder_inputs), encoder_size))
        if len(decoder_inputs) != decoder_size:
            raise ValueError("Decoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(decoder_inputs), decoder_size))
        if len(target_weights) != decoder_size:
            raise ValueError("Weights length must be equal to the one in bucket,"
                             " %d != %d." % (len(target_weights), decoder_size))

        # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
        input_feed = {}
        for l in xrange(encoder_size):
            input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
        for l in xrange(encoder_size):
            input_feed[self.encoder_inputs_r[l].name] = encoder_inputs_r[l]
        for l in xrange(decoder_size):
            input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
            input_feed[self.target_weights[l].name] = target_weights[l]

        # Since our targets are decoder inputs shifted by one, we need one more.
        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)

        # Output feed: depends on whether we do a backward step or not.
        if not forward_only:
            output_feed = [self.updates[bucket_id],  # Update Op that does SGD.
                           self.gradient_norms[bucket_id],  # Gradient norm.
                           self.losses[bucket_id]]  # Loss for this batch.
        else:
            output_feed = [self.losses[bucket_id]]  # Loss for this batch.
            for l in xrange(decoder_size):  # Output logits.
                output_feed.append(self.outputs[bucket_id][l])

        outputs = session.run(output_feed, input_feed)
        if not forward_only:
            return outputs[1], outputs[2], None  # Gradient norm, loss, no outputs.
        else:
            return None, outputs[0], outputs[1:]  # No gradient norm, loss, outputs.

    def get_batch(self, data, bucket_id):
        """Get a random batch of data from the specified bucket, prepare for step.

        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.

        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.

        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = self.buckets[bucket_id]
        encoder_inputs, encoder_inputs_r, decoder_inputs = [], [], []

        # Get a random batch of encoder and decoder inputs from data,
        # pad them if needed, reverse encoder inputs and add GO to decoder.
        for _ in xrange(self.batch_size):
            d = data[bucket_id]
            encoder_input, encoder_input_r, decoder_input = random.choice(d)

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_pad_r = [data_utils.PAD_ID] * (encoder_size - len(encoder_input_r))
            encoder_inputs.append(list(encoder_input + encoder_pad))
            encoder_inputs_r.append(list(encoder_input_r + encoder_pad_r))

            # Decoder inputs get an extra "GO" symbol, and are padded then.
            decoder_pad_size = decoder_size - len(decoder_input) - 1
            decoder_inputs.append([data_utils.GO_ID] + decoder_input +
                                  [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_encoder_inputs_r, \
            batch_decoder_inputs, batch_weights = [], [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                np.array([encoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))
            batch_encoder_inputs_r.append(
                np.array([encoder_inputs_r[batch_r_idx][length_idx]
                          for batch_r_idx in xrange(self.batch_size)], dtype=np.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                np.array([decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = np.ones(self.batch_size, dtype=np.float32)
            for batch_idx in xrange(self.batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)
        return batch_encoder_inputs, batch_encoder_inputs_r, batch_decoder_inputs, batch_weights

    def inference(self, source, source_r, target, do_decode=False):
        """
        Function to be used together with the 'model_with_buckets' function from Tensorflow's
            seq2seq module.

        Parameters
        ----------
        source: Tensor
            a Tensor corresponding to the source sentence
        source_r: Tensor
            a Tensor corresponding to the reversed source sentence
        target: Tensor
            A Tensor corresponding to the target sentence
        do_decode: boolean
            Flag indicating whether or not to use the feed_previous parameter of the
                seq2seq.embedding_attention_decoder function.

        Returns
        -------

        """
        # encoder embedding layer and bi-directional recurrent layer
        with tf.name_scope('bidirectional_encoder') as scope:
            context, decoder_initial_state = nmt.bidirectional_encoder(
                source, source_r, self.source_vocab_size, self.source_proj_size,
                self.encoder_cell, self.encoder_cell_r, self.encoder_size,
                self.batch_size, dtype=self.dtype
            )

        # decoder with attention
        with tf.name_scope('decoder_with_attention') as scope:
            # create this variable to be used inside the embedding_attention_decoder
            src_embedding = tf.Variable(
                tf.truncated_normal(
                    [self.target_vocab_size, self.target_proj_size], stddev=0.01
                ),
                name='embedding'
            )

            # First calculate a concatenation of encoder outputs to put attention on.
            top_states = [
                tf.reshape(e, [-1, 1, self.encoder_size * 2]) for e in context
                ]
            attention_states = tf.concat(1, top_states)

            # print 'Context Shape: ' + str(context[0].get_shape())
            # print 'Top states shape: ' + str(top_states[0].get_shape())
            # print 'Attention states shape:' + str(attention_states.get_shape())
            # print '\n'

            # run the decoder with attention
            outputs, states = seq2seq.embedding_attention_decoder(
                target, decoder_initial_state, attention_states,
                self.decoder_cell, self.target_vocab_size, num_heads=1,
                output_size=None, output_projection=self.output_projection,
                feed_previous=do_decode, dtype=self.dtype, scope='decoder_with_attention'
            )

        return outputs, states