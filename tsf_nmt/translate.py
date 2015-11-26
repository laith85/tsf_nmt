"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import print_function
import os
import sys
import time
import copy
import numpy
import tensorflow as tf
from tensorflow.python.platform import gfile
import data_utils
import nmt_model

# flags related to the model optimization
tf.app.flags.DEFINE_float('learning_rate', 0.5, 'Learning rate.')
tf.app.flags.DEFINE_float('learning_rate_decay_factor', 0.99, 'Learning rate decays by this much.')
tf.app.flags.DEFINE_string('optimizer', 'sgd',
                           'Name of the optimizer to use (adagrad, adam, rmsprop or sgd')

tf.app.flags.DEFINE_float('max_gradient_norm', 5.0, 'Clip gradients to this norm.')
tf.app.flags.DEFINE_integer('batch_size', 32, 'Batch size to use during training.')
tf.app.flags.DEFINE_integer('max_train_data_size', 0,
                            'Limit on the size of training data (0: no limit).')

# flags related to model architecture
tf.app.flags.DEFINE_integer('source_proj_size', 10, 'Size of source words projection.')
tf.app.flags.DEFINE_integer('target_proj_size', 10, 'Size of target words projection.')
tf.app.flags.DEFINE_integer('encoder_size', 100, 'Size of each encoder layer.')
tf.app.flags.DEFINE_integer('decoder_size', 100, 'Size of each decoderlayer.')
tf.app.flags.DEFINE_integer('num_layers', 1, 'Number of layers in each component of the model.')

# flags related to the source and target vocabularies
tf.app.flags.DEFINE_integer('src_vocab_size', 30000, 'Source language vocabulary size.')
tf.app.flags.DEFINE_integer('tgt_vocab_size', 30000, 'Target vocabulary size.')

# information about the datasets and their location
tf.app.flags.DEFINE_string('data_dir', '/home/gian/data/', 'Data directory')
tf.app.flags.DEFINE_string('train_dir', '/home/gian/train/', 'Data directory')
tf.app.flags.DEFINE_string('train_data', 'fapesp-v2.pt-en.train.tok.%s', 'Data for training.')
tf.app.flags.DEFINE_string('valid_data', 'fapesp-v2.pt-en.dev.tok.%s', 'Data for validation.')
tf.app.flags.DEFINE_string('test_data', 'fapesp-v2.pt-en.test-a.tok.%s', 'Data for testing.')
tf.app.flags.DEFINE_string('vocab_data', '', 'Training directory.')
tf.app.flags.DEFINE_string('source_lang', 'en', 'Source language extension.')
tf.app.flags.DEFINE_string('target_lang', 'pt', 'Target language extension.')

# verbosity and checkpoints
tf.app.flags.DEFINE_integer('steps_per_checkpoint', 100,
                            'How many training steps to do per checkpoint.')
tf.app.flags.DEFINE_integer('steps_per_validation', 1000,
                            'How many training steps to do between each validation.')
tf.app.flags.DEFINE_integer('steps_verbosity', 10,
                            'How many training steps to do between each information print.')

# pacience flags (learning_rate decay and early stop)
tf.app.flags.DEFINE_integer('lr_rate_patience', 3, 'How many training steps to monitor.')
tf.app.flags.DEFINE_integer('early_stop_patience', 10, 'How many training steps to monitor.')

# decoding/testing flags
tf.app.flags.DEFINE_boolean('decode_file', False, 'Set to True for decoding sentences in a file.')
tf.app.flags.DEFINE_boolean('decode_input', False, 'Set to True for interactive decoding.')

tf.app.flags.DEFINE_boolean('self_test', False, 'Run a self-test if this is set to True.')

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(5, 10), (10, 15), (20, 25), (40, 50)]


def read_data(source_path, target_path, max_size=None):
    """Read data from source and target files and put into buckets.

    Args:
      source_path: path to the files with token-ids for the source language.
      target_path: path to the file with token-ids for the target language;
        it must be aligned with the source file: n-th line contains the desired
        output for n-th line from the source_path.
      max_size: maximum number of lines to read, all other will be ignored;
        if 0 or None, data files will be read completely (no limit).

    Returns:
      data_set: a list of length len(_buckets); data_set[n] contains a list of
        (source, target) pairs read from the provided data files that fit
        into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
        len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with gfile.GFile(source_path, mode='r') as source_file:
        with gfile.GFile(target_path, mode='r') as target_file:
            source, target = source_file.readline(), target_file.readline()
            counter = 0
            while source and target and (not max_size or counter < max_size):
                counter += 1
                if counter % 10000 == 0:
                    print('  reading data line %d' % counter)
                    sys.stdout.flush()
                source_ids = [int(x) for x in source.split()]
                source_ids_r = source_ids[::-1]
                target_ids = [int(x) for x in target.split()]
                target_ids.append(data_utils.EOS_ID)
                for bucket_id, (source_size, target_size) in enumerate(_buckets):
                    if len(source_ids) < source_size and len(target_ids) < target_size:
                        data_set[bucket_id].append([source_ids, source_ids_r, target_ids])
                        break
                source, target = source_file.readline(), target_file.readline()
    return data_set


def create_model(session, forward_only):
    """Create translation model and initialize or load parameters in session."""

    decode_input = FLAGS.decode_input
    decode_file = FLAGS.decode_file

    assert (decode_input is True and decode_file is False) \
           or (decode_input is False and decode_file is True) \
           or (decode_input is False and decode_file is False), \
        'Cannot decode from input AND from file. Please choose just one option.'

    # we should set batch to 1 when decoding
    if decode_input or decode_file:
        batch = 1
    else:
        batch = FLAGS.batch_size

    model = nmt_model.NMTModel(source_vocab_size=FLAGS.src_vocab_size,
                               target_vocab_size=FLAGS.tgt_vocab_size,
                               buckets=_buckets,
                               source_proj_size=FLAGS.source_proj_size,
                               target_proj_size=FLAGS.target_proj_size,
                               encoder_size=FLAGS.encoder_size,
                               decoder_size=FLAGS.decoder_size,
                               num_layers=FLAGS.num_layers,
                               max_gradient_norm=FLAGS.max_gradient_norm,
                               batch_size=batch,
                               learning_rate=FLAGS.learning_rate,
                               learning_rate_decay_factor=FLAGS.learning_rate_decay_factor,
                               optimizer=FLAGS.optimizer,
                               forward_only=forward_only)

    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
    if ckpt and gfile.Exists(ckpt.model_checkpoint_path):
        print('Reading model parameters from %s' % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        print('Created model with fresh parameters.')
        session.run(tf.variables.initialize_all_variables())
    return model


def train():
    """Train a source->target translation model using some bilingual data."""
    # Prepare WMT data.
    print('Preparing WMT data in %s' % FLAGS.data_dir)
    src_train, tgt_train, src_dev, tgt_dev, src_test, tgt_test = data_utils.prepare_data(FLAGS)

    # summary_op = tf.merge_all_summaries()

    with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:

        # Create model.
        print('Creating layers.')
        model = create_model(sess, False)

        # tf.train.write_graph(sess.graph_def, '/home/gian/train2', 'graph.pbtxt')

        # Read data into buckets and compute their sizes.
        print('Reading development and training data (limit: %d).'
              % FLAGS.max_train_data_size)
        dev_set = read_data(src_dev, tgt_dev)
        train_set = read_data(src_train, tgt_train, FLAGS.max_train_data_size)
        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        total_loss = 0.0
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = numpy.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, encoder_inputs_r, decoder_inputs, target_weights = model.get_batch(
                train_set, bucket_id
            )
            _, step_loss, _ = model.step(sess, encoder_inputs, encoder_inputs_r, decoder_inputs,
                                         target_weights, bucket_id, False)
            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1
            total_loss += step_loss

            if current_step % FLAGS.steps_verbosity == 0:
                print('global step %d learning rate %.4f step-time %.2f avg. loss %.8f' %
                      (model.global_step.eval(), model.learning_rate.eval(), step_time,
                       total_loss/current_step))

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:

                # Print statistics for the previous epoch.
                # perplexity = math.exp(loss) if loss < 300 else float('inf')
                # print('global step %d learning rate %.4f step-time %.2f perplexity '
                #       '%.2f' % (model.global_step.eval(), model.learning_rate.eval(),
                #                 step_time, perplexity))

                # Decrease learning rate if no improvement was seen over last n times.
                prevs = FLAGS.lr_rate_patience
                if len(previous_losses) > (prevs-1) and loss > max(previous_losses[-prevs:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)

                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, 'translate.ckpt')
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0

                prevs = FLAGS.early_stop_patience
                if len(previous_losses) > (prevs-1) and loss > max(previous_losses[-prevs:]):
                    print('EARLY STOP!')
                    break

            if current_step % FLAGS.steps_per_validation == 0:

                total_eval_loss = 0.0

                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    encoder_inputs, encoder_inputs_r, decoder_inputs, target_weights = \
                        model.get_batch(dev_set, bucket_id)
                    _, eval_loss, _ = model.step(
                        sess, encoder_inputs, encoder_inputs_r, decoder_inputs,
                        target_weights, bucket_id, True
                    )

                    total_eval_loss += eval_loss

                    # eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
                    # print('  eval: bucket %d perplexity %.2f' % (bucket_id, eval_ppx))

                avg_loss = total_eval_loss / len(_buckets)
                print('  eval: averaged loss %.8f' % avg_loss)

                sys.stdout.flush()


def decode_from_stdin():
    with tf.Session() as sess:
        # Create model and load parameters.
        model = create_model(sess, True)
        model.batch_size = 1  # We decode one sentence at a time.

        # Load vocabularies.
        source_vocab_file = FLAGS.data_dir + \
                            (FLAGS.train_data % str(FLAGS.src_vocab_size)) + \
                            ('.vocab.%s' % FLAGS.source_lang)

        target_vocab_file = FLAGS.data_dir + \
                            (FLAGS.train_data % str(FLAGS.tgt_vocab_size)) + \
                            ('.vocab.%s' % FLAGS.target_lang)

        src_vocab, _ = data_utils.initialize_vocabulary(source_vocab_file)
        _, rev_tgt_vocab = data_utils.initialize_vocabulary(target_vocab_file)

        # Decode from standard input.
        sys.stdout.write('> ')
        sys.stdout.flush()
        sentence = sys.stdin.readline()

        while sentence:

            # Get token-ids for the input sentence.
            token_ids = data_utils.sentence_to_token_ids(sentence, src_vocab)

            # Which bucket does it belong to?
            bucket_id = min([b for b in xrange(len(_buckets))
                             if _buckets[b][0] > len(token_ids)])

            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, encoder_inputs_r, decoder_inputs, target_weights = model.get_batch(
                {bucket_id: [(token_ids, token_ids[::-1], [])]}, bucket_id)

            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, encoder_inputs_r, decoder_inputs,
                                             target_weights, bucket_id, True)

            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            outputs = [int(numpy.argmax(logit, axis=1)) for logit in output_logits]

            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]

            # Print out French sentence corresponding to outputs.
            print(' '.join([rev_tgt_vocab[output] for output in outputs]))
            print('> ', end='')
            sys.stdout.flush()
            sentence = sys.stdin.readline()


def decode_from_file(file_path):
    with tf.Session() as sess:

        # Create model and load parameters.
        model = create_model(sess, True)
        model.batch_size = 1  # We decode one sentence at a time.

        source_vocab_file = FLAGS.data_dir + FLAGS.vocab_file + FLAGS.source_lang
        target_vocab_file = FLAGS.data_dir + FLAGS.vocab_file + FLAGS.target_lang

        # Load vocabularies.
        src_vocab, _ = data_utils.initialize_vocabulary(source_vocab_file)
        _, rev_tgt_vocab = data_utils.initialize_vocabulary(target_vocab_file)

        # Decode from file.
        with gfile.GFile(file_path, mode='r') as source:
            with gfile.GFile(file_path + '.trans', mode='w') as destiny:
                sentence = source.readline()
                while sentence:

                    # Get token-ids for the input sentence.
                    token_ids = data_utils.sentence_to_token_ids(sentence, src_vocab)

                    # Which bucket does it belong to?
                    bucket_id = min([b for b in xrange(len(_buckets))
                                     if _buckets[b][0] > len(token_ids)])

                    # Get a 1-element batch to feed the sentence to the model.
                    encoder_inputs, encoder_inputs_r, decoder_inputs, target_weights = \
                        model.get_batch({bucket_id: [(token_ids, token_ids[::-1], [])]}, bucket_id)

                    # Get output logits for the sentence.
                    _, _, output_logits = model.step(sess, encoder_inputs, encoder_inputs_r,
                                                     decoder_inputs, target_weights, bucket_id,
                                                     True)

                    # This is a greedy decoder - outputs are just argmaxes of output_logits.
                    outputs = [int(numpy.argmax(logit, axis=1)) for logit in output_logits]

                    # If there is an EOS symbol in outputs, cut them at that point.
                    if data_utils.EOS_ID in outputs:
                        outputs = outputs[:outputs.index(data_utils.EOS_ID)]

                    # Print out sentence corresponding to outputs.
                    destiny.write(sentence.join([rev_tgt_vocab[output] for output in outputs]))
                    sentence = source.readline()


# generate sample, either with stochastic sampling or beam search. Note that,
# this function iteratively calls f_init and f_next functions.
def gen_sample(f_init, f_next, x, k=1, maxlen=30):

    sample = []
    sample_score = []

    live_k = 1
    dead_k = 0

    hyp_samples = [[]] * live_k
    hyp_scores = numpy.zeros(live_k).astype('float32')
    hyp_states = []

    # get initial state of decoder rnn and encoder context
    ret = f_init(x)
    next_state, ctx0 = ret[0], ret[1]
    next_w = -1 * numpy.ones((1,)).astype('int64')  # bos indicator

    for ii in xrange(maxlen):
        ctx = numpy.tile(ctx0, [live_k, 1])
        inps = [next_w, ctx, next_state]
        ret = f_next(*inps)
        next_p, next_w, next_state = ret[0], ret[1], ret[2]

        cand_scores = hyp_scores[:, None] - numpy.log(next_p)
        cand_flat = cand_scores.flatten()
        ranks_flat = cand_flat.argsort()[:(k - dead_k)]

        voc_size = next_p.shape[1]
        trans_indices = ranks_flat / voc_size
        word_indices = ranks_flat % voc_size
        costs = cand_flat[ranks_flat]

        new_hyp_samples = []
        new_hyp_scores = numpy.zeros(k - dead_k).astype('float32')
        new_hyp_states = []

        for idx, [ti, wi] in enumerate(zip(trans_indices, word_indices)):
            new_hyp_samples.append(hyp_samples[ti] + [wi])
            new_hyp_scores[idx] = copy.copy(costs[ti])
            new_hyp_states.append(copy.copy(next_state[ti]))

        # check the finished samples
        new_live_k = 0
        hyp_samples = []
        hyp_scores = []
        hyp_states = []

        for idx in xrange(len(new_hyp_samples)):
            if new_hyp_samples[idx][-1] == 0:
                sample.append(new_hyp_samples[idx])
                sample_score.append(new_hyp_scores[idx])
                dead_k += 1
            else:
                new_live_k += 1
                hyp_samples.append(new_hyp_samples[idx])
                hyp_scores.append(new_hyp_scores[idx])
                hyp_states.append(new_hyp_states[idx])
        hyp_scores = numpy.array(hyp_scores)
        live_k = new_live_k

        if new_live_k < 1:
            break
        if dead_k >= k:
            break

        next_w = numpy.array([w[-1] for w in hyp_samples])
        next_state = numpy.array(hyp_states)

    # dump every remaining one
    if live_k > 0:
        for idx in xrange(live_k):
            sample.append(hyp_samples[idx])
            sample_score.append(hyp_scores[idx])

    return sample, sample_score


def self_test():
    """Test the translation model."""
    # with tf.Session() as sess:
    #     print('Self-test for neural translation model.')
    #     # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
    #     model = seq2seq_model.Seq2SeqModel(10, 10, [(3, 3), (6, 6)], 32, 2,
    #                                        5.0, 32, 0.3, 0.99, num_samples=8)
    #     sess.run(tf.variables.initialize_all_variables())
    #
    #     # Fake data set for both the (3, 3) and (6, 6) bucket.
    #     data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
    #                 [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
    #     for _ in xrange(5):  # Train the fake model for 5 steps.
    #         bucket_id = random.choice([0, 1])
    #         encoder_inputs, decoder_inputs, target_weights = model.get_batch(
    #             data_set, bucket_id)
    #         model.step(sess, encoder_inputs, decoder_inputs, target_weights,
    #                    bucket_id, False)


def main(_):
    if FLAGS.self_test:
        self_test()
    elif FLAGS.decode_input:
        decode_from_stdin()
    elif FLAGS.decode_file:
        decode_from_file('')
    else:
        train()


if __name__ == '__main__':
    tf.app.run()