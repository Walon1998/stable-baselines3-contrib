from typing import Generator, Optional, Tuple, Union

import numpy as np
import torch as th
from gym import spaces
from stable_baselines3.common.buffers import DictRolloutBuffer, RolloutBuffer
from stable_baselines3.common.vec_env import VecNormalize

from sb3_contrib.common.recurrent.type_aliases import (
    RecurrentDictRolloutBufferSamples,
    RecurrentRolloutBufferSamples,
    RNNStates,
)


class RecurrentRolloutBuffer(RolloutBuffer):
    """
    Rollout buffer that also stores the invalid action masks associated with each observation.

    :param buffer_size: Max number of element in the buffer
    :param observation_space: Observation space
    :param action_space: Action space
    :param device:
    :param gae_lambda: Factor for trade-off of bias vs variance for Generalized Advantage Estimator
        Equivalent to classic advantage when set to 1.
    :param gamma: Discount factor
    :param n_envs: Number of parallel environments
    """

    def __init__(
            self,
            buffer_size: int,
            observation_space: spaces.Space,
            action_space: spaces.Space,
            lstm_states: Tuple[np.ndarray, np.ndarray],
            device: Union[th.device, str] = "cpu",
            gae_lambda: float = 1,
            gamma: float = 0.99,
            n_envs: int = 1,
            sampling_strategy: str = "default",  # "default" or "per_env",
            lstm_unroll_length: int = None
    ):
        self.lstm_states = lstm_states
        self.initial_lstm_states = None
        self.sampling_strategy = sampling_strategy
        self.starts, self.ends = None, None

        if lstm_unroll_length is None:
            self.lstm_unroll_length = buffer_size
        else:
            self.lstm_unroll_length = lstm_unroll_length

        super().__init__(buffer_size, observation_space, action_space, device, gae_lambda, gamma, n_envs)

    def reset(self):
        super().reset()
        self.hidden_states_pi = np.zeros_like(self.lstm_states[0])
        self.cell_states_pi = np.zeros_like(self.lstm_states[1])
        # self.hidden_states_vf = np.zeros_like(self.lstm_states[0])
        # self.cell_states_vf = np.zeros_like(self.lstm_states[1])

    def add(self,
            obs: np.ndarray,
            action: np.ndarray,
            reward: np.ndarray,
            episode_start: np.ndarray,
            value: th.Tensor,
            log_prob: th.Tensor,
            lstm_states_0_cpu,
            lstm_states_1_cpu, ) -> None:
        """
        :param hidden_states: LSTM cell and hidden state
        """
        self.hidden_states_pi[self.pos] = np.array(lstm_states_0_cpu.numpy(), dtype=np.float32)
        self.cell_states_pi[self.pos] = np.array(lstm_states_1_cpu.numpy(), dtype=np.float32)
        # self.hidden_states_vf[self.pos] = np.array(lstm_states.vf[0].cpu().numpy(), dtype=np.float32)
        # self.cell_states_vf[self.pos] = np.array(lstm_states.vf[1].cpu().numpy(), dtype=np.float32)

        super().add(obs,
                    action,
                    reward,
                    episode_start,
                    value,
                    log_prob)

    def get(self, batch_size: Optional[int] = None) -> Generator[RecurrentRolloutBufferSamples, None, None]:
        assert self.full, ""

        # Prepare the data
        if not self.generator_ready:
            # hidden_state_shape = (self.n_steps, lstm.num_layers, self.n_envs, lstm.hidden_size)
            # swap first to (self.n_steps, self.n_envs, lstm.num_layers, lstm.hidden_size)
            for tensor in ["hidden_states_pi", "cell_states_pi"]:
                self.__dict__[tensor] = self.__dict__[tensor].swapaxes(1, 2)

            for tensor in [
                "observations",
                "actions",
                "values",
                "log_probs",
                "advantages",
                "returns",
                "hidden_states_pi",
                "cell_states_pi",
                "episode_starts",
            ]:
                self.__dict__[tensor] = self.swap_and_flatten(self.__dict__[tensor])
            self.generator_ready = True

        # Return everything, don't create minibatches
        if batch_size is None:
            batch_size = self.buffer_size * self.n_envs

        # Sampling strategy that allows any mini batch size but requires
        # more complexity and use of padding
        if self.sampling_strategy == "default":
            # No shuffling
            # indices = np.arange(self.buffer_size * self.n_envs)
            # Trick to shuffle a bit: keep the sequence order
            # but split the indices in two
            split_index = np.random.randint(self.buffer_size * self.n_envs)
            indices = np.arange(self.buffer_size * self.n_envs)
            indices = np.concatenate((indices[split_index:], indices[:split_index]))

            env_change = np.zeros(self.buffer_size * self.n_envs).reshape(self.buffer_size, self.n_envs)
            # Flag first timestep as change of environment
            env_change[0, :] = 1.0
            env_change = self.swap_and_flatten(env_change)

            start_idx = 0
            while start_idx < self.buffer_size * self.n_envs:
                batch_inds = indices[start_idx: start_idx + batch_size]
                yield self._get_samples(batch_inds, env_change)
                start_idx += batch_size
            return

        # ==== OpenAI Baselines way of sampling, constraint in the batch size and number of environments ====

        assert self.buffer_size % self.lstm_unroll_length == 0, "{},{}".format(self.buffer_size, self.lstm_unroll_length)

        assert batch_size >= self.n_envs, "{},{}".format(batch_size, self.n_envs)

        assert batch_size % self.n_envs == 0, "{},{}".format(batch_size, self.n_envs)

        stack_size = int(batch_size / self.n_envs)

        assert batch_size * self.lstm_unroll_length == self.buffer_size * self.n_envs, "{},{},{},{}".format(batch_size, self.lstm_unroll_length, self.buffer_size, self.n_envs)

        iterations = int((self.buffer_size / self.lstm_unroll_length) / stack_size)

        observations_reshape = self.observations.reshape((self.n_envs, -1, 159))
        actions_reshape = self.actions.reshape((self.n_envs, -1, 7))
        old_values_reshape = self.values.reshape((self.n_envs, -1, 1))
        old_log_prob_reshape = self.log_probs.reshape((self.n_envs, -1, 1))
        advantages_prob_reshape = self.advantages.reshape((self.n_envs, -1, 1))
        returns_prob_reshape = self.returns.reshape((self.n_envs, -1, 1))
        hidden_states_pi_prob_reshape = self.hidden_states_pi.reshape((self.n_envs, -1, 1, 1024))
        cell_states_pi_pi_prob_reshape = self.cell_states_pi.reshape((self.n_envs, -1, 1, 1024))
        episode_starts_prob_reshape = self.episode_starts.reshape((self.n_envs, -1, 1))

        for i in range(iterations):
            obs_stack = []
            action_stack = []
            values_stack = []
            log_probs_stack = []
            advantages_stack = []
            returns_stack = []
            hidden_states_pi_stack = []
            cell_states_pi_stack = []
            episode_starts_pi_stack = []

            for j in range(stack_size):
                obs_stack.append(observations_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                action_stack.append(actions_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                values_stack.append(old_values_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                log_probs_stack.append(old_log_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                advantages_stack.append(advantages_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                returns_stack.append(returns_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])
                hidden_states_pi_stack.append(hidden_states_pi_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length, :, :])
                cell_states_pi_stack.append(cell_states_pi_pi_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length, :, :])
                episode_starts_pi_stack.append(episode_starts_prob_reshape[:, (i * stack_size + j) * self.lstm_unroll_length:(i * stack_size + j + 1) * self.lstm_unroll_length, :])

            lstm_states_pi = (
                np.concatenate(hidden_states_pi_stack, axis=0).reshape(1, batch_size, -1),
                np.concatenate(cell_states_pi_stack, axis=0).reshape(1, batch_size, -1),
            )

            lstm_states_pi = (self.to_torch(lstm_states_pi[0]), self.to_torch(lstm_states_pi[1]))

            yield RecurrentRolloutBufferSamples(
                observations=self.to_torch(np.concatenate(obs_stack, axis=0).reshape(-1, 159)),
                actions=self.to_torch(np.concatenate(action_stack, axis=0).reshape(-1, 7)),
                old_values=self.to_torch(np.concatenate(values_stack, axis=0).flatten()),
                old_log_prob=self.to_torch(np.concatenate(log_probs_stack, axis=0).flatten()),
                advantages=self.to_torch(np.concatenate(advantages_stack, axis=0).flatten()),
                returns=self.to_torch(np.concatenate(returns_stack, axis=0).flatten()),
                lstm_states=RNNStates(lstm_states_pi, None),
                episode_starts=self.to_torch(np.concatenate(episode_starts_pi_stack, axis=0).flatten()),
            )

    def pad(self, tensor: np.ndarray) -> th.Tensor:
        seq = [self.to_torch(tensor[start: end + 1]) for start, end in zip(self.starts, self.ends)]
        return th.nn.utils.rnn.pad_sequence(seq)

    def _get_samples(
            self,
            batch_inds: np.ndarray,
            env_change: np.ndarray,
            env: Optional[VecNormalize] = None,
    ) -> RecurrentRolloutBufferSamples:
        # Create sequence if env change too
        seq_start = np.logical_or(self.episode_starts[batch_inds], env_change[batch_inds]).flatten()
        # First index is always the beginning of a sequence
        seq_start[0] = True
        self.starts = np.where(seq_start == True)[0]  # noqa: E712
        self.ends = np.concatenate([(self.starts - 1)[1:], np.array([len(batch_inds)])])

        n_layers = self.hidden_states_pi.shape[1]
        n_seq = len(self.starts)
        max_length = self.pad(self.actions[batch_inds]).shape[0]
        # TODO: output mask to not backpropagate everywhere
        padded_batch_size = n_seq * max_length
        lstm_states_pi = (
            # (n_steps, n_layers, n_envs, dim) -> (n_layers, n_seq, dim)
            self.hidden_states_pi[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
            self.cell_states_pi[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
        )
        # lstm_states_vf = (
        #     # (n_steps, n_layers, n_envs, dim) -> (n_layers, n_seq, dim)
        #     self.hidden_states_vf[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
        #     self.cell_states_vf[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
        # )
        lstm_states_pi = (self.to_torch(lstm_states_pi[0]), self.to_torch(lstm_states_pi[1]))
        # lstm_states_vf = (self.to_torch(lstm_states_vf[0]), self.to_torch(lstm_states_vf[1]))

        return RecurrentRolloutBufferSamples(
            observations=self.pad(self.observations[batch_inds]).swapaxes(0, 1).reshape((padded_batch_size,) + self.obs_shape),
            actions=self.pad(self.actions[batch_inds]).swapaxes(0, 1).reshape((padded_batch_size,) + self.actions.shape[1:]),
            old_values=self.pad(self.values[batch_inds]).swapaxes(0, 1).flatten(),
            old_log_prob=self.pad(self.log_probs[batch_inds]).swapaxes(0, 1).flatten(),
            advantages=self.pad(self.advantages[batch_inds]).swapaxes(0, 1).flatten(),
            returns=self.pad(self.returns[batch_inds]).swapaxes(0, 1).flatten(),
            lstm_states=RNNStates(lstm_states_pi, None),
            episode_starts=self.pad(self.episode_starts[batch_inds]).swapaxes(0, 1).flatten(),
        )


class RecurrentDictRolloutBuffer(DictRolloutBuffer):
    """
    Dict Rollout buffer used in on-policy algorithms like A2C/PPO.
    Extends the RolloutBuffer to use dictionary observations

    It corresponds to ``buffer_size`` transitions collected
    using the current policy.
    This experience will be discarded after the policy update.
    In order to use PPO objective, we also store the current value of each state
    and the log probability of each taken action.

    The term rollout here refers to the model-free notion and should not
    be used with the concept of rollout used in model-based RL or planning.
    Hence, it is only involved in policy and value function training but not action selection.

    :param buffer_size: Max number of element in the buffer
    :param observation_space: Observation space
    :param action_space: Action space
    :param device:
    :param gae_lambda: Factor for trade-off of bias vs variance for Generalized Advantage Estimator
        Equivalent to classic advantage when set to 1.
    :param gamma: Discount factor
    :param n_envs: Number of parallel environments
    """

    def __init__(
            self,
            buffer_size: int,
            observation_space: spaces.Space,
            action_space: spaces.Space,
            lstm_states: Tuple[np.ndarray, np.ndarray],
            device: Union[th.device, str] = "cpu",
            gae_lambda: float = 1,
            gamma: float = 0.99,
            n_envs: int = 1,
            sampling_strategy: str = "default",  # "default" or "per_env"
    ):
        self.lstm_states = lstm_states
        self.initial_lstm_states = None
        self.sampling_strategy = sampling_strategy
        assert sampling_strategy == "default", "'per_env' strategy not supported with dict obs"
        super().__init__(buffer_size, observation_space, action_space, device, gae_lambda, gamma, n_envs=n_envs)

    def reset(self):
        super().reset()
        self.hidden_states_pi = np.zeros_like(self.lstm_states[0])
        self.cell_states_pi = np.zeros_like(self.lstm_states[1])
        self.hidden_states_vf = np.zeros_like(self.lstm_states[0])
        self.cell_states_vf = np.zeros_like(self.lstm_states[1])

    def add(self, *args, lstm_states: RNNStates, **kwargs) -> None:
        """
        :param hidden_states: LSTM cell and hidden state
        """
        self.hidden_states_pi[self.pos] = np.array(lstm_states.pi[0].cpu().numpy())
        self.cell_states_pi[self.pos] = np.array(lstm_states.pi[1].cpu().numpy())
        self.hidden_states_vf[self.pos] = np.array(lstm_states.vf[0].cpu().numpy())
        self.cell_states_vf[self.pos] = np.array(lstm_states.vf[1].cpu().numpy())

        super().add(*args, **kwargs)

    def get(self, batch_size: Optional[int] = None) -> Generator[RecurrentDictRolloutBufferSamples, None, None]:
        assert self.full, ""

        # Prepare the data
        if not self.generator_ready:
            # hidden_state_shape = (self.n_steps, lstm.num_layers, self.n_envs, lstm.hidden_size)
            # swap first to (self.n_steps, self.n_envs, lstm.num_layers, lstm.hidden_size)
            for tensor in ["hidden_states_pi", "cell_states_pi", "hidden_states_vf", "cell_states_vf"]:
                self.__dict__[tensor] = self.__dict__[tensor].swapaxes(1, 2)

            for key, obs in self.observations.items():
                self.observations[key] = self.swap_and_flatten(obs)

            for tensor in [
                "actions",
                "values",
                "log_probs",
                "advantages",
                "returns",
                "hidden_states_pi",
                "cell_states_pi",
                "hidden_states_vf",
                "cell_states_vf",
                "episode_starts",
            ]:
                self.__dict__[tensor] = self.swap_and_flatten(self.__dict__[tensor])
            self.generator_ready = True

        # Return everything, don't create minibatches
        if batch_size is None:
            batch_size = self.buffer_size * self.n_envs

        # No shuffling:
        # indices = np.arange(self.buffer_size * self.n_envs)
        # Trick to shuffle a bit: keep the sequence order
        # but split the indices in two
        split_index = np.random.randint(self.buffer_size * self.n_envs)
        indices = np.arange(self.buffer_size * self.n_envs)
        indices = np.concatenate((indices[split_index:], indices[:split_index]))

        env_change = np.zeros(self.buffer_size * self.n_envs).reshape(self.buffer_size, self.n_envs)
        # Flag first timestep as change of environment
        env_change[0, :] = 1.0
        env_change = self.swap_and_flatten(env_change)

        start_idx = 0
        while start_idx < self.buffer_size * self.n_envs:
            batch_inds = indices[start_idx: start_idx + batch_size]
            yield self._get_samples(batch_inds, env_change)
            start_idx += batch_size

    def pad(self, tensor: np.ndarray) -> th.Tensor:
        seq = [self.to_torch(tensor[start: end + 1]) for start, end in zip(self.starts, self.ends)]
        return th.nn.utils.rnn.pad_sequence(seq)

    def _get_samples(
            self,
            batch_inds: np.ndarray,
            env_change: np.ndarray,
            env: Optional[VecNormalize] = None,
    ) -> RecurrentDictRolloutBufferSamples:
        # Create sequence if env change too
        seq_start = np.logical_or(self.episode_starts[batch_inds], env_change[batch_inds]).flatten()
        # First index is always the beginning of a sequence
        seq_start[0] = True
        self.starts = np.where(seq_start == True)[0]  # noqa: E712
        self.ends = np.concatenate([(self.starts - 1)[1:], np.array([len(batch_inds)])])

        n_layers = self.hidden_states_pi.shape[1]
        n_seq = len(self.starts)
        max_length = self.pad(self.actions[batch_inds]).shape[0]
        # TODO: output mask to not backpropagate everywhere
        padded_batch_size = n_seq * max_length
        lstm_states_pi = (
            # (n_steps, n_layers, n_envs, dim) -> (n_layers, n_seq, dim)
            self.hidden_states_pi[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
            self.cell_states_pi[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
        )
        lstm_states_vf = (
            # (n_steps, n_layers, n_envs, dim) -> (n_layers, n_seq, dim)
            self.hidden_states_vf[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
            self.cell_states_vf[batch_inds][seq_start == True].reshape(n_layers, n_seq, -1),  # noqa: E712
        )
        lstm_states_pi = (self.to_torch(lstm_states_pi[0]), self.to_torch(lstm_states_pi[1]))
        lstm_states_vf = (self.to_torch(lstm_states_vf[0]), self.to_torch(lstm_states_vf[1]))

        observations = {key: self.pad(obs[batch_inds]) for (key, obs) in self.observations.items()}
        observations = {
            key: obs.swapaxes(0, 1).reshape((padded_batch_size,) + self.obs_shape[key]) for (key, obs) in observations.items()
        }

        return RecurrentDictRolloutBufferSamples(
            observations=observations,
            actions=self.pad(self.actions[batch_inds]).swapaxes(0, 1).reshape((padded_batch_size,) + self.actions.shape[1:]),
            old_values=self.pad(self.values[batch_inds]).swapaxes(0, 1).flatten(),
            old_log_prob=self.pad(self.log_probs[batch_inds]).swapaxes(0, 1).flatten(),
            advantages=self.pad(self.advantages[batch_inds]).swapaxes(0, 1).flatten(),
            returns=self.pad(self.returns[batch_inds]).swapaxes(0, 1).flatten(),
            lstm_states=RNNStates(lstm_states_pi, lstm_states_vf),
            episode_starts=self.pad(self.episode_starts[batch_inds]).swapaxes(0, 1).flatten(),
        )
