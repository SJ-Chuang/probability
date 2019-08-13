# Copyright 2018 The TensorFlow Probability Authors.
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
# ============================================================================
"""Tests of the No U-Turn Sampler."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


# Dependency imports
from absl.testing import parameterized
import numpy as np

import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp

from tensorflow_probability.python.distributions.internal import statistical_testing as st
from tensorflow_probability.python.internal import assert_util
from tensorflow.python.framework import test_util  # pylint: disable=g-direct-tensorflow-import

tfb = tfp.bijectors
tfd = tfp.distributions


@tf.function(autograph=False)
def run_nuts_chain(event_size, batch_size, num_steps, initial_state=None):
  def target_log_prob_fn(event):
    with tf.name_scope('nuts_test_target_log_pro'):
      return tfd.MultivariateNormalDiag(
          tf.zeros(event_size),
          scale_identity_multiplier=1.).log_prob(event)

  if initial_state is None:
    initial_state = tf.zeros([batch_size, event_size])

  kernel = tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
      target_log_prob_fn,
      step_size=[0.3],
      unrolled_leapfrog_steps=2,
      max_tree_depth=4,
      seed=1)

  chain_state, extra = tfp.mcmc.sample_chain(
      num_results=num_steps,
      num_burnin_steps=0,
      # Intentionally pass a list argument to test that singleton lists are
      # handled reasonably (c.f. assert_univariate_target_conservation, which
      # uses an unwrapped singleton).
      current_state=[initial_state],
      kernel=kernel,
      parallel_iterations=1)

  return chain_state, extra.leapfrogs_taken


def assert_univariate_target_conservation(test, target_d, step_size):
  # Sample count limited partly by memory reliably available on Forge.  The test
  # remains reasonable even if the nuts recursion limit is severely curtailed
  # (e.g., 3 or 4 levels), so use that to recover some memory footprint and bump
  # the sample count.
  num_samples = int(5e4)
  num_steps = 1
  strm = tfd.SeedStream(salt='univariate_nuts_test', seed=1)
  initialization = target_d.sample([num_samples], seed=strm())

  @tf.function(autograph=False)
  def run_chain():
    nuts = tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
        target_d.log_prob,
        step_size=step_size,
        max_tree_depth=3,
        unrolled_leapfrog_steps=2,
        seed=strm())
    result, _ = tfp.mcmc.sample_chain(
        num_results=num_steps,
        num_burnin_steps=0,
        current_state=initialization,
        kernel=nuts)
    return result

  result = run_chain()
  test.assertAllEqual([num_steps, num_samples], result.shape)
  answer = result[0]
  check_cdf_agrees = st.assert_true_cdf_equal_by_dkwm(
      answer, target_d.cdf, false_fail_rate=1e-6)
  check_enough_power = assert_util.assert_less(
      st.min_discrepancy_of_true_cdfs_detectable_by_dkwm(
          num_samples, false_fail_rate=1e-6, false_pass_rate=1e-6), 0.025)
  movement = tf.abs(answer - initialization)
  test.assertAllEqual([num_samples], movement.shape)
  # This movement distance (1 * step_size) was selected by reducing until 100
  # runs with independent seeds all passed.
  check_movement = assert_util.assert_greater_equal(
      tf.reduce_mean(movement), 1 * step_size)
  return (check_cdf_agrees, check_enough_power, check_movement)


def assert_mvn_target_conservation(event_size, batch_size, **kwargs):
  initialization = tfd.MultivariateNormalFullCovariance(
      loc=tf.zeros(event_size),
      covariance_matrix=tf.eye(event_size)).sample(
          batch_size, seed=4)
  samples, _ = run_nuts_chain(
      event_size, batch_size, num_steps=1,
      initial_state=initialization, **kwargs)
  answer = samples[0][-1]
  check_cdf_agrees = (
      st.assert_multivariate_true_cdf_equal_on_projections_two_sample(
          answer, initialization, num_projections=100, false_fail_rate=1e-6))
  check_sample_shape = assert_util.assert_equal(
      tf.shape(answer)[0], batch_size)
  movement = tf.linalg.norm(answer - initialization, axis=-1)
  # This movement distance (0.3) was copied from the univariate case.
  check_movement = assert_util.assert_greater_equal(
      tf.reduce_mean(movement), 0.3)
  check_enough_power = assert_util.assert_less(
      st.min_discrepancy_of_true_cdfs_detectable_by_dkwm_two_sample(
          batch_size, batch_size, false_fail_rate=1e-8, false_pass_rate=1e-6),
      0.055)
  return (
      check_cdf_agrees,
      check_sample_shape,
      check_movement,
      check_enough_power,
  )


@test_util.run_all_in_graph_and_eager_modes
class NutsTest(parameterized.TestCase, tf.test.TestCase):

  def testUnivariateNormalTargetConservation(self):
    normal_dist = tfd.Normal(loc=1., scale=2.)
    self.evaluate(assert_univariate_target_conservation(
        self, normal_dist, step_size=0.2))

  def testLogitBetaTargetConservation(self):
    logit_beta_dist = tfb.Invert(tfb.Sigmoid())(
        tfd.Beta(concentration0=1., concentration1=2.))
    self.evaluate(assert_univariate_target_conservation(
        self, logit_beta_dist, step_size=0.2))

  def testSigmoidBetaTargetConservation(self):
    # Not inverting the sigmoid bijector makes a kooky distribution, but nuts
    # should still conserve it (with a smaller step size).
    sigmoid_beta_dist = tfb.Identity(tfb.Sigmoid())(
        tfd.Beta(concentration0=1., concentration1=2.))
    self.evaluate(assert_univariate_target_conservation(
        self, sigmoid_beta_dist, step_size=0.02))

  @parameterized.parameters(
      (3, 50000,),
      # (5, 2,),
  )
  def testMultivariateNormalNd(self, event_size, batch_size):
    self.evaluate(assert_mvn_target_conservation(event_size, batch_size))

  def testLatentsOfMixedRank(self):
    batch_size = 10
    num_steps = 100

    init0 = [tf.ones([batch_size, 6])]
    def log_prob0(x):
      return tfd.Independent(
          tfd.Normal(tf.range(6, dtype=tf.float32),
                     tf.constant(1.)),
          reinterpreted_batch_ndims=1).log_prob(x)
    kernel0 = tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
        log_prob0,
        step_size=0.3,
        unrolled_leapfrog_steps=2,
        max_tree_depth=4,
        seed=1)
    results0, _ = tfp.mcmc.sample_chain(
        num_results=num_steps,
        num_burnin_steps=0,
        current_state=init0,
        kernel=kernel0,
        parallel_iterations=1)

    init1 = [tf.ones([batch_size,]),
             tf.ones([batch_size, 1]),
             tf.ones([batch_size, 2, 2])]
    def log_prob1(state0, state1, state2):
      return (
          tfd.Normal(tf.constant(0.), tf.constant(1.)).log_prob(state0)
          + tfd.Independent(
              tfd.Normal(tf.constant([1.]), tf.constant(1.)),
              reinterpreted_batch_ndims=1).log_prob(state1)
          + tfd.Independent(
              tfd.Normal(tf.constant([[2., 3.], [4., 5.]]), tf.constant(1.)),
              reinterpreted_batch_ndims=2).log_prob(state2)
      )
    kernel1 = tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
        log_prob1,
        step_size=0.3,
        unrolled_leapfrog_steps=2,
        max_tree_depth=4,
        seed=1)
    results1, extra1 = tfp.mcmc.sample_chain(
        num_results=num_steps,
        num_burnin_steps=0,
        current_state=init1,
        kernel=kernel1,
        parallel_iterations=1)
    self.evaluate([results1, extra1])
    self.assertAllClose(
        tf.reduce_mean(results0[0], axis=[0, 1]),
        tf.squeeze(tf.concat(
            [tf.reshape(tf.reduce_mean(x, axis=[0, 1]), [-1, 1])
             for x in results1], axis=0)),
        atol=0.1, rtol=0.1)

  @parameterized.parameters(
      (1000, 5, 3),
      # (500, 1000, 20),
  )
  def testMultivariateNormalNdConvergence(self, nsamples, nchains, nd):
    strm = tfd.SeedStream(1, salt='MultivariateNormalNdConvergence')
    theta0 = np.zeros((nchains, nd))
    mu = np.arange(nd)
    w = np.random.randn(nd, nd) * 0.1
    cov = w * w.T + np.diagflat(np.arange(nd) + 1.)
    step_size = np.random.rand(nchains, 1) * 0.1 + 1.

    @tf.function(autograph=False)
    def run_nuts(mu, scale_tril, step_size, nsamples, state):
      def target_log_prob_fn(event):
        with tf.name_scope('nuts_test_target_log_prob'):
          return tfd.MultivariateNormalTriL(
              loc=tf.cast(mu, dtype=tf.float64),
              scale_tril=tf.cast(scale_tril, dtype=tf.float64)).log_prob(event)

      nuts = tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
          target_log_prob_fn,
          step_size=[step_size],
          max_tree_depth=5,
          seed=strm())

      def trace_fn(_, pkr):
        return (pkr.is_accepted, pkr.leapfrogs_taken)

      [x], (is_accepted, leapfrogs_taken) = tfp.mcmc.sample_chain(
          num_results=nsamples,
          num_burnin_steps=0,
          current_state=[tf.cast(state, dtype=tf.float64)],
          kernel=nuts,
          trace_fn=trace_fn,
          parallel_iterations=1)

      leapfrogs_taken_ = leapfrogs_taken[1:] - leapfrogs_taken[:-1]

      return (
          tf.shape(x),
          # We'll average over samples (dim=0) and chains (dim=1).
          tf.reduce_mean(x, axis=[0, 1]),
          tfp.stats.covariance(x, sample_axis=[0, 1]),
          leapfrogs_taken_[is_accepted[1:]])

    sample_shape, sample_mean, sample_cov, leapfrogs_taken = self.evaluate(
        run_nuts(mu, np.linalg.cholesky(cov), step_size, nsamples, theta0))

    self.assertAllEqual(sample_shape, [nsamples, nchains, nd])
    self.assertAllClose(mu, sample_mean, atol=0.1, rtol=0.1)
    self.assertAllClose(cov, sample_cov, atol=0.15, rtol=0.15)
    # Test early stopping in tree building
    self.assertTrue(
        np.any(np.isin(np.asarray([5, 9, 11, 13]), np.unique(leapfrogs_taken))))

  def testSampleEndtoEnd(self):
    """An end-to-end test of sampling using NUTS."""
    strm = tfd.SeedStream(1, salt='EndtoEndTest')
    predictors = tf.cast([
        201., 244., 47., 287., 203., 58., 210., 202., 198., 158., 165., 201.,
        157., 131., 166., 160., 186., 125., 218., 146.
    ], tf.float32)
    obs = tf.cast([
        592., 401., 583., 402., 495., 173., 479., 504., 510., 416., 393., 442.,
        317., 311., 400., 337., 423., 334., 533., 344.
    ], tf.float32)
    y_sigma = tf.cast([
        61., 25., 38., 15., 21., 15., 27., 14., 30., 16., 14., 25., 52., 16.,
        34., 31., 42., 26., 16., 22.
    ], tf.float32)

    # Robust linear regression model
    robust_lm = tfd.JointDistributionSequential(
        [
            tfd.Normal(loc=0., scale=1.),  # b0
            tfd.Normal(loc=0., scale=1.),  # b1
            tfd.HalfNormal(5.),  # df
            lambda df, b1, b0: tfd.Independent(  # pylint: disable=g-long-lambda
                tfd.StudentT(  # Likelihood
                    df=df[:, None],
                    loc=b0[:, None] + b1[:, None] * predictors[None, :],
                    scale=y_sigma[None, :])),
        ],
        validate_args=True)

    log_prob = lambda b0, b1, df: robust_lm.log_prob([b0, b1, df, obs])
    init_step_size = [1., .2, .5]
    step_size0 = [tf.cast(x, dtype=tf.float32) for x in init_step_size]

    number_of_steps, burnin, nchain = 100, 50, 50

    @tf.function(autograph=False)
    def run_chain():
      # random initialization of the starting postion of each chain
      b0, b1, df, _ = robust_lm.sample(nchain, seed=strm())

      # bijector to map contrained parameters to real
      unconstraining_bijectors = [
          tfb.Identity(),
          tfb.Identity(),
          tfb.Exp(),
      ]

      def trace_fn(_, pkr):
        return (pkr.inner_results.inner_results.step_size,
                pkr.inner_results.inner_results.log_accept_ratio)

      kernel = tfp.mcmc.DualAveragingStepSizeAdaptation(
          tfp.mcmc.TransformedTransitionKernel(
              inner_kernel=tfp.experimental.mcmc.NoUTurnSamplerUnrolled(
                  target_log_prob_fn=log_prob,
                  step_size=step_size0,
                  seed=strm()),
              bijector=unconstraining_bijectors),
          num_adaptation_steps=burnin,
          step_size_setter_fn=lambda pkr, new_step_size: pkr._replace(  # pylint: disable=g-long-lambda
              inner_results=pkr.inner_results._replace(step_size=new_step_size)
          ),
          step_size_getter_fn=lambda pkr: pkr.inner_results.step_size,
          log_accept_prob_getter_fn=lambda pkr: pkr.inner_results.
          log_accept_ratio,
      )

      # Sampling from the chain and get diagnostics
      mcmc_trace, (step_size, log_accept_ratio) = tfp.mcmc.sample_chain(
          num_results=number_of_steps,
          num_burnin_steps=burnin,
          current_state=[b0, b1, df],
          kernel=kernel,
          trace_fn=trace_fn)
      rhat = tfp.mcmc.potential_scale_reduction(mcmc_trace)
      return (
          [s[-1] for s in step_size],  # final step size
          tf.reduce_mean(tf.exp(log_accept_ratio)),
          [tf.reduce_mean(rhat_) for rhat_ in rhat],  # average rhat
      )

    # Sample from posterior distribution and get diagnostic
    [
        final_step_size, average_accept_ratio, average_rhat
    ] = self.evaluate(run_chain())

    # Check that step size adaptation reduced the initial step size
    self.assertAllLess(
        np.asarray(final_step_size) - np.asarray(init_step_size), 0.)
    # Check that average acceptance ratio is close to target
    self.assertAllClose(
        average_accept_ratio,
        .8 * np.ones_like(average_accept_ratio),
        atol=0.1, rtol=0.1)
    # Check that mcmc sample quality is acceptable with tuning
    self.assertAllClose(
        average_rhat, np.ones_like(average_rhat), atol=0.05, rtol=0.05)


if __name__ == '__main__':
  tf.test.main()
