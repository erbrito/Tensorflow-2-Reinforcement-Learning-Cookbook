#!/usr/bin/env/ python
# Proximal Policy Optimizatin (PPO) agent training script
# Chapter 3, TensorFlow 2 Reinforcement Learning Cookbook | Praveen Palanisamy

import argparse
import os
from datetime import datetime

import gym
import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import Dense, Input, Lambda

tf.keras.backend.set_floatx("float64")


parser = argparse.ArgumentParser(prog="TFRL-Cookbook-Ch3-PPO")
parser.add_argument("--env", default="Pendulum-v0")
parser.add_argument("--update-freq", type=int, default=5)
parser.add_argument("--epochs", type=int, default=3)
parser.add_argument("--actor-lr", type=float, default=0.0005)
parser.add_argument("--critic-lr", type=float, default=0.001)
parser.add_argument("--clip-ratio", type=float, default=0.1)
parser.add_argument("--gae-lambda", type=float, default=0.95)
parser.add_argument("--gamma", type=float, default=0.99)
parser.add_argument("--logdir", default="logs")

args = parser.parse_args()
logdir = os.path.join(
    args.logdir, parser.prog, args.env, datetime.now().strftime("%Y%m%d-%H%M%S")
)
print(f"Saving training logs to:{logdir}")
writer = tf.summary.create_file_writer(logdir)


class Actor:
    def __init__(self, state_dim, action_dim, action_bound, std_bound):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.action_bound = action_bound
        self.std_bound = std_bound
        self.model = self.nn_model()
        self.opt = tf.keras.optimizers.Adam(args.actor_lr)

    def nn_model(self):
        state_input = Input((self.state_dim,))
        dense_1 = Dense(32, activation="relu")(state_input)
        dense_2 = Dense(32, activation="relu")(dense_1)
        out_mu = Dense(self.action_dim, activation="tanh")(dense_2)
        mu_output = Lambda(lambda x: x * self.action_bound)(out_mu)
        std_output = Dense(self.action_dim, activation="softplus")(dense_2)
        return tf.keras.models.Model(state_input, [mu_output, std_output])

    def get_action(self, state):
        state = np.reshape(state, [1, self.state_dim])
        mu, std = self.model.predict(state)
        action = np.random.normal(mu[0], std[0], size=self.action_dim)
        action = np.clip(action, -self.action_bound, self.action_bound)
        log_policy = self.log_pdf(mu, std, action)

        return log_policy, action

    def log_pdf(self, mu, std, action):
        std = tf.clip_by_value(std, self.std_bound[0], self.std_bound[1])
        var = std ** 2
        log_policy_pdf = -0.5 * (action - mu) ** 2 / var - 0.5 * tf.math.log(
            var * 2 * np.pi
        )
        return tf.reduce_sum(log_policy_pdf, 1, keepdims=True)

    def compute_loss(self, log_old_policy, log_new_policy, actions, gaes):
        ratio = tf.exp(log_new_policy - tf.stop_gradient(log_old_policy))
        gaes = tf.stop_gradient(gaes)
        clipped_ratio = tf.clip_by_value(
            ratio, 1.0 - args.clip_ratio, 1.0 + args.clip_ratio
        )
        surrogate = -tf.minimum(ratio * gaes, clipped_ratio * gaes)
        return tf.reduce_mean(surrogate)

    def train(self, log_old_policy, states, actions, gaes):
        with tf.GradientTape() as tape:
            mu, std = self.model(states, training=True)
            log_new_policy = self.log_pdf(mu, std, actions)
            loss = self.compute_loss(log_old_policy, log_new_policy, actions, gaes)
        grads = tape.gradient(loss, self.model.trainable_variables)
        self.opt.apply_gradients(zip(grads, self.model.trainable_variables))
        return loss


class Critic:
    def __init__(self, state_dim):
        self.state_dim = state_dim
        self.model = self.nn_model()
        self.opt = tf.keras.optimizers.Adam(args.critic_lr)

    def nn_model(self):
        return tf.keras.Sequential(
            [
                Input((self.state_dim,)),
                Dense(32, activation="relu"),
                Dense(32, activation="relu"),
                Dense(16, activation="relu"),
                Dense(1, activation="linear"),
            ]
        )

    def compute_loss(self, v_pred, td_targets):
        mse = tf.keras.losses.MeanSquaredError()
        return mse(td_targets, v_pred)

    def train(self, states, td_targets):
        with tf.GradientTape() as tape:
            v_pred = self.model(states, training=True)
            # assert v_pred.shape == td_targets.shape
            loss = self.compute_loss(v_pred, tf.stop_gradient(td_targets))
        grads = tape.gradient(loss, self.model.trainable_variables)
        self.opt.apply_gradients(zip(grads, self.model.trainable_variables))
        return loss


class Agent:
    def __init__(self, env):
        self.env = env
        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space.shape[0]
        self.action_bound = self.env.action_space.high[0]
        self.std_bound = [1e-2, 1.0]

        self.actor_opt = tf.keras.optimizers.Adam(args.actor_lr)
        self.critic_opt = tf.keras.optimizers.Adam(args.critic_lr)
        self.actor = Actor(
            self.state_dim, self.action_dim, self.action_bound, self.std_bound
        )
        self.critic = Critic(self.state_dim)

    def gae_target(self, rewards, v_values, next_v_value, done):
        n_step_targets = np.zeros_like(rewards)
        gae = np.zeros_like(rewards)
        gae_cumulative = 0
        forward_val = 0

        if not done:
            forward_val = next_v_value

        for k in reversed(range(0, len(rewards))):
            delta = rewards[k] + args.gamma * forward_val - v_values[k]
            gae_cumulative = args.gamma * args.gae_lambda * gae_cumulative + delta
            gae[k] = gae_cumulative
            forward_val = v_values[k]
            n_step_targets[k] = gae[k] + v_values[k]
        return gae, n_step_targets

    def train(self, max_episodes=1000):
        with writer.as_default():
            for ep in range(max_episodes):
                state_batch = []
                action_batch = []
                reward_batch = []
                old_policy_batch = []

                episode_reward, done = 0, False

                state = self.env.reset()

                while not done:
                    # self.env.render()
                    log_old_policy, action = self.actor.get_action(state)

                    next_state, reward, done, _ = self.env.step(action)

                    state = np.reshape(state, [1, self.state_dim])
                    action = np.reshape(action, [1, 1])
                    next_state = np.reshape(next_state, [1, self.state_dim])
                    reward = np.reshape(reward, [1, 1])
                    log_old_policy = np.reshape(log_old_policy, [1, 1])

                    state_batch.append(state)
                    action_batch.append(action)
                    reward_batch.append((reward + 8) / 8)
                    old_policy_batch.append(log_old_policy)

                    if len(state_batch) >= args.update_freq or done:
                        states = np.array([state.squeeze() for state in state_batch])
                        actions = np.array(
                            [action.squeeze() for action in action_batch]
                        )
                        rewards = np.array(
                            [reward.squeeze() for reward in reward_batch]
                        )
                        old_policies = np.array(
                            [old_pi.squeeze() for old_pi in old_policy_batch]
                        )

                        v_values = self.critic.model.predict(states)
                        next_v_value = self.critic.model.predict(next_state)

                        gaes, td_targets = self.gae_target(
                            rewards, v_values, next_v_value, done
                        )
                        actor_losses, critic_losses = [], []
                        for epoch in range(args.epochs):
                            actor_loss = self.actor.train(
                                old_policies, states, actions, gaes
                            )
                            actor_losses.append(actor_loss)
                            critic_loss = self.critic.train(states, td_targets)
                            critic_losses.append(critic_loss)
                        # Plot mean actor & critic losses on every update
                        tf.summary.scalar("actor_loss", np.mean(actor_losses), step=ep)
                        tf.summary.scalar(
                            "critic_loss", np.mean(critic_losses), step=ep
                        )

                        state_batch = []
                        action_batch = []
                        reward_batch = []
                        old_policy_batch = []

                    episode_reward += reward[0][0]
                    state = next_state[0]

                print(f"Episode#{ep} Reward:{episode_reward}")
                tf.summary.scalar("episode_reward", episode_reward, step=ep)


if __name__ == "__main__":
    env_name = "Pendulum-v0"
    env = gym.make(env_name)
    agent = Agent(env)
    agent.train()
