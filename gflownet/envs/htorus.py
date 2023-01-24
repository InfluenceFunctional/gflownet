"""
Classes to represent hyper-torus environments
"""
from typing import List, Tuple
import itertools
import numpy as np
import numpy.typing as npt
import pandas as pd
import matplotlib.pyplot as plt
import torch
from gflownet.utils.common import torch2np
from gflownet.envs.base import GFlowNetEnv
from torch.distributions import Categorical, Uniform, VonMises, Bernoulli
from torchtyping import TensorType
from sklearn.neighbors import KernelDensity


class HybridTorus(GFlowNetEnv):
    """
    Continuous (hybrid: discrete and continuous) hyper-torus environment in which the
    action space consists of the selection of which dimension d to increment increment
    and of the angle of dimension d. The trajectory is of fixed length length_traj.

    The states space is the concatenation of the angle (in radians and within [0, 2 *
    pi]) at each dimension and the number of actions.

    Attributes
    ----------
    ndim : int
        Dimensionality of the torus

    length_traj : int
       Fixed length of the trajectory.
    """

    def __init__(
        self,
        n_dim=2,
        length_traj=1,
        do_nonzero_source_prob=True,
        fixed_distribution=dict,
        random_distribution=dict,
        env_id=None,
        reward_beta=1,
        reward_norm=1.0,
        reward_norm_std_mult=0,
        reward_func="boltzmann",
        denorm_proxy=False,
        energies_stats=None,
        proxy=None,
        oracle=None,
        **kwargs,
    ):
        super(HybridTorus, self).__init__(
            env_id=env_id,
            reward_beta=reward_beta,
            reward_norm=reward_norm,
            reward_norm_std_mult=reward_norm_std_mult,
            reward_func=reward_func,
            energies_stats=energies_stats,
            denorm_proxy=denorm_proxy,
            proxy=proxy,
            oracle=oracle,
            **kwargs,
        )
        self.continuous = True
        self.n_dim = n_dim
        self.eos = self.n_dim
        self.length_traj = length_traj
        # Parameters of fixed policy distribution
        if do_nonzero_source_prob:
            self.n_params_per_dim = 4
        else:
            self.n_params_per_dim = 3
        self.vonmises_concentration_epsilon = 1e-3
        # Initialize angles and state attributes
        self.reset()
        self.source = self.angles.copy()
        self.action_space = self.get_actions_space()
        self.fixed_policy_output = self.get_policy_output(fixed_distribution)
        self.random_policy_output = self.get_policy_output(random_distribution)
        self.policy_output_dim = len(self.fixed_policy_output)
        self.policy_input_dim = len(self.state2policy())
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)
        # Oracle
        self.state2oracle = self.state2proxy
        self.statebatch2oracle = self.statebatch2proxy
        # Setup proxy
        self.proxy.n_dim = self.n_dim

    def get_actions_space(self):
        """
        Constructs list with all possible actions. The actions are tuples with two
        values: (dimension, magnitude) where dimension indicates the index of the
        dimension on which the action is to be performed and magnitude indicates the
        increment of the angle in radians.
        """
        actions = [(d, None) for d in range(self.n_dim)]
        actions += [(self.eos, 0.0)]
        return actions

    def get_policy_output(self, params: dict):
        """
        Defines the structure of the output of the policy model, from which an
        action is to be determined or sampled, by returning a vector with a fixed
        random policy.

        For each dimension of the hyper-torus, the output of the policy should return
        1) a logit, for the categorical distribution over dimensions and 2) the
        location and 3) the concentration of the projected normal distribution to
        sample the increment of the angle and 4) (if do_nonzero_source_prob is True)
        the logit of a Bernoulli distribution to model the (discrete) backward
        probability of returning to the value of the source node.

        Thus:
        - n_params_per_dim = 4 if do_nonzero_source_prob is True
        - n_params_per_dim = 3 if do_nonzero_source_prob is False

        Therefore, the output of the policy model has dimensionality D x
        n_params_per_dim + 1, where D is the number of dimensions, and the elements of
        the output vector are:
        - d * n_params_per_dim: logit of dimension d
        - d * n_params_per_dim + 1: location of Von Mises distribution for dimension d
        - d * n_params_per_dim + 2: log concentration of Von Mises distribution for dimension d
        - d * n_params_per_dim + 3: logit of Bernoulli distribution
        with d in [0, ..., D]
        """
        policy_output = np.ones(self.n_dim * self.n_params_per_dim + 1)
        policy_output[1 :: self.n_params_per_dim] = params.vonmises_mean
        policy_output[2 :: self.n_params_per_dim] = params.vonmises_concentration
        return policy_output

    def get_mask_invalid_actions_forward(self, state=None, done=None):
        """
        Returns a vector with the length of the discrete part of the action space + 1:
        True if action is invalid going forward given the current state, False
        otherwise.
        """
        if state is None:
            state = self.state.copy()
        if done is None:
            done = self.done
        if done:
            return [True for _ in range(len(self.action_space))]
        if state[-1] >= self.length_traj:
            mask = [True for _ in range(len(self.action_space))]
            mask[-1] = False
        else:
            mask = [False for _ in range(len(self.action_space))]
            mask[-1] = True
        return mask

    def get_mask_invalid_actions_backward(self, state=None, done=None, parents_a=None):
        """
        Returns a vector with the length of the discrete part of the action space + 1:
        True if action is invalid going backward given the current state, False
        otherwise.
        """
        if state is None:
            state = self.state.copy()
        if done is None:
            done = self.done
        if done:
            mask = [True for _ in range(len(self.action_space))]
            mask[-1] = False
        else:
            mask = [False for _ in range(len(self.action_space))]
            mask[-1] = True
        # Catch cases where it would not be possible to reach the initial state
        noninit_states = [s for s, ss in zip(state[:-1], self.source) if s != ss]
        if len(noninit_states) > state[-1]:
            print("This point in the code should never be reached!")
        elif len(noninit_states) == state[-1] and len(noninit_states) >= state[-1] - 1:
            mask = [
                True if s == ss else m
                for m, s, ss in zip(mask, state[:-1], self.source)
            ] + [mask[-1]]
        return mask

    def true_density(self):
        # TODO
        # Return pre-computed true density if already stored
        if self._true_density is not None:
            return self._true_density
        # Calculate true density
        all_x = self.get_all_terminating_states()
        all_oracle = self.state2oracle(all_x)
        rewards = self.oracle(all_oracle)
        self._true_density = (
            rewards / rewards.sum(),
            rewards,
            list(map(tuple, all_x)),
        )
        return self._true_density

    def statebatch2proxy(
        self, states: List[List]
    ) -> TensorType["batch", "state_proxy_dim"]:
        """
        Prepares a batch of states in "GFlowNet format" for the proxy: a tensor where
        each state is a row of length n_dim with an angle in radians. The n_actions
        item is removed.
        """
        return torch.tensor(states, device=self.device)[:, :-1]

    def statetorch2proxy(
        self, states: TensorType["batch", "state_dim"]
    ) -> TensorType["batch", "state_proxy_dim"]:
        """
        Prepares a batch of states in torch "GFlowNet format" for the proxy.
        """
        return states[:, :-1]

    def state2policy(self, state: List = None) -> List:
        """
        Returns the state as is.
        """
        if state is None:
            state = self.state.copy()
        return state

    def policy2state(self, state_policy: List) -> List:
        """
        Returns the input as is.
        """
        return state_policy

    def state2readable(self, state: List) -> str:
        """
        Converts a state (a list of positions) into a human-readable string
        representing a state. Angles are converted into degrees in [0, 360]
        """
        angles = np.array(state[:-1])
        angles = angles * 180 / np.pi
        angles = str(angles).replace("(", "[").replace(")", "]").replace(",", "")
        n_actions = str(int(state[-1]))
        return angles + " | " + n_actions

    def readable2state(self, readable: str) -> List:
        """
        Converts a human-readable string representing a state into a state as a list of
        positions. Angles are converted back to radians.
        """
        pair = readable.split(" | ")
        angles = [np.float32(el) * np.pi / 180 for el in pair[0].strip("[]").split(" ")]
        n_actions = [int(pair[1])]
        return angles + n_actions

    def reset(self, env_id=None):
        """
        Resets the environment.
        """
        self.angles = [0.0 for _ in range(self.n_dim)]
        # TODO: do step encoding as in Sasha's code?
        self.n_actions = 0
        # States are the concatenation of the angle state and number of actions
        self.state = self.angles + [self.n_actions]
        self.done = False
        self.id = env_id
        return self

    def get_parents(
        self, state: List = None, done: bool = None, action: Tuple[int, float] = None
    ) -> Tuple[List[List], List[Tuple[int, float]]]:
        """
        Determines all parents and actions that lead to state.

        Args
        ----
        state : list
            Representation of a state, as a list of length n_angles where each element
            is the position at each dimension.

        done : bool
            Whether the trajectory is done. If None, done is taken from instance.

        action : int
            Last action performed

        Returns
        -------
        parents : list
            List of parents in state format

        actions : list
            List of actions that lead to state for each parent in parents
        """
        # TODO: we might have to include the valid discrete backward actions for the
        # backward sampling. Otherwise, implement backward mask.
        if state is None:
            state = self.state.copy()
        if done is None:
            done = self.done
        if done:
            return [state], [(self.eos, 0.0)]
        else:
            state[action[0]] = (state[action[0]] - action[1]) % (2 * np.pi)
            state[-1] -= 1
            parents = [state]
            return parents, [action]

    def sample_actions(
        self,
        policy_outputs: TensorType["n_states", "policy_output_dim"],
        sampling_method: str = "policy",
        mask_invalid_actions: TensorType["n_states", "n_dim"] = None,
        temperature_logits: float = 1.0,
        loginf: float = 1000,
    ) -> Tuple[List[Tuple], TensorType["n_states"]]:
        """
        Samples a batch of actions from a batch of policy outputs.
        """
        device = policy_outputs.device
        n_states = policy_outputs.shape[0]
        ns_range = torch.arange(n_states).to(device)
        # Sample dimensions
        if sampling_method == "uniform":
            logits_dims = torch.ones(n_states, self.policy_output_dim).to(device)
        elif sampling_method == "policy":
            logits_dims = policy_outputs[:, 0 :: self.n_params_per_dim]
            logits_dims /= temperature_logits
        if mask_invalid_actions is not None:
            logits_dims[mask_invalid_actions] = -loginf
        dimensions = Categorical(logits=logits_dims).sample()
        logprobs_dim = self.logsoftmax(logits_dims)[ns_range, dimensions]
        # Sample angle increments
        ns_range_noeos = ns_range[dimensions != self.eos]
        dimensions_noeos = dimensions[dimensions != self.eos]
        angles = torch.zeros(n_states).to(device)
        logprobs_angles = torch.zeros(n_states).to(device)
        if len(dimensions_noeos) > 0:
            if sampling_method == "uniform":
                distr_angles = Uniform(
                    torch.zeros(len(ns_range_noeos)),
                    2 * torch.pi * torch.ones(len(ns_range_noeos)),
                )
            elif sampling_method == "policy":
                locations = policy_outputs[:, 1 :: self.n_params_per_dim][
                    ns_range_noeos, dimensions_noeos
                ]
                concentrations = policy_outputs[:, 2 :: self.n_params_per_dim][
                    ns_range_noeos, dimensions_noeos
                ]
                distr_angles = VonMises(
                    locations,
                    torch.exp(concentrations) + self.vonmises_concentration_epsilon,
                )
            angles[ns_range_noeos] = distr_angles.sample()
            logprobs_angles[ns_range_noeos] = distr_angles.log_prob(
                angles[ns_range_noeos]
            )
        # Combined probabilities
        logprobs = logprobs_dim + logprobs_angles
        # Build actions
        actions = [
            (dimension, angle)
            for dimension, angle in zip(dimensions.tolist(), angles.tolist())
        ]
        return actions, logprobs

    def get_logprobs(
        self,
        policy_outputs: TensorType["n_states", "policy_output_dim"],
        is_forward: bool,
        actions: TensorType["n_states", 2],
        states_target: TensorType["n_states", "policy_input_dim"],
        mask_invalid_actions: TensorType["batch_size", "policy_output_dim"] = None,
        loginf: float = 1000,
    ) -> TensorType["batch_size"]:
        """
        Computes log probabilities of actions given policy outputs and actions.
        """
        device = policy_outputs.device
        dimensions, angles = zip(*actions)
        dimensions = torch.LongTensor([d.long() for d in dimensions]).to(device)
        angles = torch.FloatTensor(angles).to(device)
        n_states = policy_outputs.shape[0]
        ns_range = torch.arange(n_states).to(device)
        # Dimensions
        logits_dims = policy_outputs[:, 0 :: self.n_params_per_dim]
        if mask_invalid_actions is not None:
            logits_dims[mask_invalid_actions] = -loginf
        logprobs_dim = self.logsoftmax(logits_dims)[ns_range, dimensions]
        # Angle increments
        # Cases where p(angle) should be computed (nofix):
        # - A: Dimension != eos, and (
        # - B: (# dimensions different to source != # steps, or
        # - C: Angle of selected dimension != source) or
        # - D: is_forward)
        # nofix: A & ((B | C) | D)
        # Mixing p(angle) with discrete probability of going backwards to the source
        # The mixed (backward) probability of sampling angle, p(angle_mixed) is:
        # - p(angle) * p(no_source), if angle of target != source
        # - p(source), if angle of target == source
        # Mixing should be applied if p(angle) is computed AND is backward:
        source = torch.tensor(self.source, device=device)
        source_aux = torch.tensor(self.source + [-1], device=device)
        nsource_ne_nsteps = torch.ne(
            torch.sum(torch.ne(states_target[:, :-1], source), axis=1),
            states_target[:, -1],
        )
        angledim_ne_source = torch.ne(
            states_target[ns_range, dimensions], source_aux[dimensions]
        )
        noeos = torch.ne(dimensions, self.eos)
        nofix_indices = torch.logical_and(
            torch.logical_or(nsource_ne_nsteps, angledim_ne_source) | is_forward, noeos
        )
        logprobs_angles = torch.zeros(n_states).to(device)
        logprobs_nosource = torch.zeros(n_states).to(device)
        if torch.any(nofix_indices):
            ns_range_nofix = ns_range[nofix_indices]
            dimensions_nofix = dimensions[nofix_indices]
            locations = policy_outputs[:, 1 :: self.n_params_per_dim][
                ns_range_nofix, dimensions_nofix
            ]
            concentrations = policy_outputs[:, 2 :: self.n_params_per_dim][
                ns_range_nofix, dimensions_nofix
            ]
            distr_angles = VonMises(
                locations,
                torch.exp(concentrations) + self.vonmises_concentration_epsilon,
            )
            logprobs_angles[ns_range_nofix] = distr_angles.log_prob(
                angles[ns_range_nofix]
            )
            if self.n_params_per_dim == 4 and (not is_forward):
                logits_nosource = policy_outputs[:, 3 :: self.n_params_per_dim][
                    ns_range_nofix, dimensions_nofix
                ]
                distr_nosource = Bernoulli(logits=logits_nosource)
                logprobs_nosource[ns_range_nofix] = distr_nosource.log_prob(
                    angledim_ne_source[ns_range_nofix].to(self.float)
                )
        # Combined probabilities
        logprobs = logprobs_dim + logprobs_angles + logprobs_nosource
        return logprobs

    def step(
        self, action: Tuple[int, float]
    ) -> Tuple[List[float], Tuple[int, float], bool]:
        """
        Executes step given an action.

        Args
        ----
        action : tuple
            Action to be executed. An action is a tuple with two values:
            (dimension, magnitude).

        Returns
        -------
        self.state : list
            The sequence after executing the action

        action : int
            Action executed

        valid : bool
            False, if the action is not allowed for the current state, e.g. stop at the
            root state
        """
        if self.done:
            return self.state, action, False
        # If only possible action is eos, then force eos
        # If the number of actions is equal to maximum trajectory length
        elif self.n_actions == self.length_traj:
            self.done = True
            self.n_actions += 1
            return self.state, (self.eos, 0.0), True
        # If action is not eos, then perform action
        elif action[0] != self.eos:
            self.n_actions += 1
            self.state[action[0]] += action[1]
            self.state[action[0]] = self.state[action[0]] % (2 * np.pi)
            self.state[-1] = self.n_actions
            return self.state, action, True
        # If action is eos, then it is invalid
        else:
            return self.state, action, False

    def get_grid_terminating_states(self, n_states: int) -> List[List]:
        n_per_dim = int(np.ceil(n_states ** (1 / self.n_dim)))
        linspaces = [np.linspace(0, 2 * np.pi, n_per_dim) for _ in range(self.n_dim)]
        angles = list(itertools.product(*linspaces))
        states = [list(el) + [self.length_traj] for el in angles]
        return states

    # TODO: make generic for all environments
    def sample_from_reward(
        self, n_samples: int, epsilon=1e-4
    ) -> TensorType["n_samples", "state_dim"]:
        """
        Rejection sampling  with proposal the uniform distribution in [0, 2pi]]^n_dim.

        Returns a tensor in GFloNet (state) format.
        """
        samples_final = []
        max_reward = self.proxy2reward(torch.tensor([self.proxy.min])).to(self.device)
        while len(samples_final) < n_samples:
            angles_uniform = (
                torch.rand(
                    (n_samples, self.n_dim), dtype=self.float, device=self.device
                )
                * 2
                * np.pi
            )
            samples = torch.cat(
                (
                    angles_uniform,
                    torch.ones((angles_uniform.shape[0], 1)).to(angles_uniform),
                ),
                axis=1,
            )
            rewards = self.reward_torchbatch(samples)
            mask = (
                torch.rand(n_samples, dtype=self.float, device=self.device)
                * (max_reward + epsilon)
                < rewards
            )
            samples_accepted = samples[mask, :]
            samples_final.extend(samples_accepted[-(n_samples - len(samples_final)) :])
        return torch.vstack(samples_final)

    def fit_kde(self, samples, kernel="gaussian", bandwidth=0.1):
        aug_samples = []
        for add_0 in [0, -2 * np.pi, 2 * np.pi]:
            for add_1 in [0, -2 * np.pi, 2 * np.pi]:
                aug_samples.append(
                    np.stack([samples[:, 0] + add_0, samples[:, 1] + add_1], axis=1)
                )
        aug_samples = np.concatenate(aug_samples)
        kde = KernelDensity(kernel=kernel, bandwidth=bandwidth).fit(aug_samples)
        return kde

    def plot_reward_samples(
        self, samples, alpha=0.5, low=-np.pi * 0.5, high=2.5 * np.pi, dpi=150
    ):
        x = np.linspace(low, high, 201)
        y = np.linspace(low, high, 201)
        xx, yy = np.meshgrid(x, y)
        X = np.stack([xx, yy], axis=-1)
        samples_mesh = torch.tensor(
            X.reshape(-1, 2), dtype=self.float, device=self.device
        )
        rewards = torch2np(self.proxy2reward(self.proxy(samples_mesh)))
        # Init figure
        fig, ax = plt.subplots()
        fig.set_dpi(dpi)
        # Plot reward contour
        h = ax.contourf(xx, yy, rewards.reshape(xx.shape), alpha=alpha)
        ax.axis("scaled")
        fig.colorbar(h, ax=ax)
        ax.plot([0, 0], [0, 2 * np.pi], "-w", alpha=alpha)
        ax.plot([0, 2 * np.pi], [0, 0], "-w", alpha=alpha)
        ax.plot([2 * np.pi, 2 * np.pi], [2 * np.pi, 0], "-w", alpha=alpha)
        ax.plot([2 * np.pi, 0], [2 * np.pi, 2 * np.pi], "-w", alpha=alpha)
        # Plot samples
        extra_samples = []
        for add_0 in [0, -2 * np.pi, 2 * np.pi]:
            for add_1 in [0, -2 * np.pi, 2 * np.pi]:
                if not (add_0 == add_1 == 0):
                    extra_samples.append(
                        np.stack([samples[:, 0] + add_0, samples[:, 1] + add_1], axis=1)
                    )
        extra_samples = np.concatenate(extra_samples)
        ax.scatter(samples[:, 0], samples[:, 1], alpha=alpha)
        ax.scatter(extra_samples[:, 0], extra_samples[:, 1], alpha=alpha, color="white")
        ax.grid()
        # Set tight layout
        plt.tight_layout()
        return fig

    def plot_kde(self, kde, alpha=0.5, low=-np.pi * 0.5, high=2.5 * np.pi, dpi=150):
        x = np.linspace(0, 2 * np.pi, 101)
        y = np.linspace(0, 2 * np.pi, 101)
        xx, yy = np.meshgrid(x, y)
        X = np.stack([xx, yy], axis=-1)
        Z = np.exp(kde.score_samples(X.reshape(-1, 2))).reshape(xx.shape)
        # Init figure
        fig, ax = plt.subplots()
        fig.set_dpi(dpi)
        # Plot KDE
        h = ax.contourf(xx, yy, Z, alpha=alpha)
        ax.axis("scaled")
        fig.colorbar(h, ax=ax)
        # Set tight layout
        plt.tight_layout()
        return fig
