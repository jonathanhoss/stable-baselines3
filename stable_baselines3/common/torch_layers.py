from typing import Optional, Union

import gymnasium as gym
import torch as th
from gymnasium import spaces
from torch import nn

from stable_baselines3.common.preprocessing import get_flattened_obs_dim, is_image_space
from stable_baselines3.common.type_aliases import TensorDict
from stable_baselines3.common.utils import get_device
import torch
import torch.nn as nn
import numpy as np
from torch_geometric.nn import GINConv
from torch_geometric.nn import aggr


class BaseFeaturesExtractor(nn.Module):
    """
    Base class that represents a features extractor.

    :param observation_space: The observation space of the environment
    :param features_dim: Number of features extracted.
    """

    def __init__(self, observation_space: gym.Space, features_dim: int = 0) -> None:
        super().__init__()
        assert features_dim > 0
        self._observation_space = observation_space
        self._features_dim = features_dim

    @property
    def features_dim(self) -> int:
        """The number of features that the extractor outputs."""
        return self._features_dim


class FlattenExtractor(BaseFeaturesExtractor):
    """
    Feature extract that flatten the input.
    Used as a placeholder when feature extraction is not needed.

    :param observation_space: The observation space of the environment
    """

    def __init__(self, observation_space: gym.Space) -> None:
        super().__init__(observation_space, get_flattened_obs_dim(observation_space))
        self.flatten = nn.Flatten()

    def forward(self, observations: th.Tensor) -> th.Tensor:
        return self.flatten(observations)


class NatureCNN(BaseFeaturesExtractor):
    """
    CNN from DQN Nature paper:
        Mnih, Volodymyr, et al.
        "Human-level control through deep reinforcement learning."
        Nature 518.7540 (2015): 529-533.

    :param observation_space: The observation space of the environment
    :param features_dim: Number of features extracted.
        This corresponds to the number of unit for the last layer.
    :param normalized_image: Whether to assume that the image is already normalized
        or not (this disables dtype and bounds checks): when True, it only checks that
        the space is a Box and has 3 dimensions.
        Otherwise, it checks that it has expected dtype (uint8) and bounds (values in [0, 255]).
    """

    def __init__(
        self,
        observation_space: gym.Space,
        features_dim: int = 512,
        normalized_image: bool = False,
    ) -> None:
        assert isinstance(observation_space, spaces.Box), (
            "NatureCNN must be used with a gym.spaces.Box ",
            f"observation space, not {observation_space}",
        )
        super().__init__(observation_space, features_dim)
        # We assume CxHxW images (channels first)
        # Re-ordering will be done by pre-preprocessing or wrapper
        assert is_image_space(observation_space, check_channels=False, normalized_image=normalized_image), (
            "You should use NatureCNN "
            f"only with images not with {observation_space}\n"
            "(you are probably using `CnnPolicy` instead of `MlpPolicy` or `MultiInputPolicy`)\n"
            "If you are using a custom environment,\n"
            "please check it using our env checker:\n"
            "https://stable-baselines3.readthedocs.io/en/master/common/env_checker.html.\n"
            "If you are using `VecNormalize` or already normalized channel-first images "
            "you should pass `normalize_images=False`: \n"
            "https://stable-baselines3.readthedocs.io/en/master/guide/custom_env.html"
        )
        n_input_channels = observation_space.shape[0]
        self.cnn = nn.Sequential(
            nn.Conv2d(n_input_channels, 32, kernel_size=8, stride=4, padding=0),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2, padding=0),
            nn.ReLU(),
            nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=0),
            nn.ReLU(),
            nn.Flatten(),
        )

        # Compute shape by doing one forward pass
        with th.no_grad():
            n_flatten = self.cnn(th.as_tensor(observation_space.sample()[None]).float()).shape[1]

        self.linear = nn.Sequential(nn.Linear(n_flatten, features_dim), nn.ReLU())

    def forward(self, observations: th.Tensor) -> th.Tensor:
        return self.linear(self.cnn(observations))


def create_mlp(
    input_dim: int,
    output_dim: int,
    net_arch: list[int],
    activation_fn: type[nn.Module] = nn.ReLU,
    squash_output: bool = False,
    with_bias: bool = True,
    pre_linear_modules: Optional[list[type[nn.Module]]] = None,
    post_linear_modules: Optional[list[type[nn.Module]]] = None,
) -> list[nn.Module]:
    """
    Create a multi layer perceptron (MLP), which is
    a collection of fully-connected layers each followed by an activation function.

    :param input_dim: Dimension of the input vector
    :param output_dim: Dimension of the output (last layer, for instance, the number of actions)
    :param net_arch: Architecture of the neural net
        It represents the number of units per layer.
        The length of this list is the number of layers.
    :param activation_fn: The activation function
        to use after each layer.
    :param squash_output: Whether to squash the output using a Tanh
        activation function
    :param with_bias: If set to False, the layers will not learn an additive bias
    :param pre_linear_modules: List of nn.Module to add before the linear layers.
        These modules should maintain the input tensor dimension (e.g. BatchNorm).
        The number of input features is passed to the module's constructor.
        Compared to post_linear_modules, they are used before the output layer (output_dim > 0).
    :param post_linear_modules: List of nn.Module to add after the linear layers
        (and before the activation function). These modules should maintain the input
        tensor dimension (e.g. Dropout, LayerNorm). They are not used after the
        output layer (output_dim > 0). The number of input features is passed to
        the module's constructor.
    :return: The list of layers of the neural network
    """

    pre_linear_modules = pre_linear_modules or []
    post_linear_modules = post_linear_modules or []

    modules = []
    if len(net_arch) > 0:
        # BatchNorm maintains input dim
        for module in pre_linear_modules:
            modules.append(module(input_dim))

        modules.append(nn.Linear(input_dim, net_arch[0], bias=with_bias))

        # LayerNorm, Dropout maintain output dim
        for module in post_linear_modules:
            modules.append(module(net_arch[0]))

        modules.append(activation_fn())

    for idx in range(len(net_arch) - 1):
        for module in pre_linear_modules:
            modules.append(module(net_arch[idx]))

        modules.append(nn.Linear(net_arch[idx], net_arch[idx + 1], bias=with_bias))

        for module in post_linear_modules:
            modules.append(module(net_arch[idx + 1]))

        modules.append(activation_fn())

    if output_dim > 0:
        last_layer_dim = net_arch[-1] if len(net_arch) > 0 else input_dim
        # Only add BatchNorm before output layer
        for module in pre_linear_modules:
            modules.append(module(last_layer_dim))

        modules.append(nn.Linear(last_layer_dim, output_dim, bias=with_bias))
    if squash_output:
        modules.append(nn.Tanh())
    return modules


class MlpExtractor(nn.Module):
    """
    Constructs an MLP that receives the output from a previous features extractor (i.e. a CNN) or directly
    the observations (if no features extractor is applied) as an input and outputs a latent representation
    for the policy and a value network.

    The ``net_arch`` parameter allows to specify the amount and size of the hidden layers.
    It can be in either of the following forms:
    1. ``dict(vf=[<list of layer sizes>], pi=[<list of layer sizes>])``: to specify the amount and size of the layers in the
        policy and value nets individually. If it is missing any of the keys (pi or vf),
        zero layers will be considered for that key.
    2. ``[<list of layer sizes>]``: "shortcut" in case the amount and size of the layers
        in the policy and value nets are the same. Same as ``dict(vf=int_list, pi=int_list)``
        where int_list is the same for the actor and critic.

    .. note::
        If a key is not specified or an empty list is passed ``[]``, a linear network will be used.

    :param feature_dim: Dimension of the feature vector (can be the output of a CNN)
    :param net_arch: The specification of the policy and value networks.
        See above for details on its formatting.
    :param activation_fn: The activation function to use for the networks.
    :param device: PyTorch device.
    """

    def __init__(
        self,
        feature_dim: int,
        net_arch: Union[list[int], dict[str, list[int]]],
        activation_fn: type[nn.Module],
        device: Union[th.device, str] = "auto",
    ) -> None:
        super().__init__()
        device = get_device(device)
        policy_net: list[nn.Module] = []
        value_net: list[nn.Module] = []
        last_layer_dim_pi = feature_dim
        last_layer_dim_vf = feature_dim

        # save dimensions of layers in policy and value nets
        if isinstance(net_arch, dict):
            # Note: if key is not specified, assume linear network
            pi_layers_dims = net_arch.get("pi", [])  # Layer sizes of the policy network
            vf_layers_dims = net_arch.get("vf", [])  # Layer sizes of the value network
        else:
            pi_layers_dims = vf_layers_dims = net_arch
        # Iterate through the policy layers and build the policy net
        for curr_layer_dim in pi_layers_dims:
            policy_net.append(nn.Linear(last_layer_dim_pi, curr_layer_dim))
            policy_net.append(activation_fn())
            last_layer_dim_pi = curr_layer_dim
        # Iterate through the value layers and build the value net
        for curr_layer_dim in vf_layers_dims:
            value_net.append(nn.Linear(last_layer_dim_vf, curr_layer_dim))
            value_net.append(activation_fn())
            last_layer_dim_vf = curr_layer_dim

        # Save dim, used to create the distributions
        self.latent_dim_pi = last_layer_dim_pi
        self.latent_dim_vf = last_layer_dim_vf

        # Create networks
        # If the list of layers is empty, the network will just act as an Identity module
        self.policy_net = nn.Sequential(*policy_net).to(device)
        self.value_net = nn.Sequential(*value_net).to(device)

    def forward(self, features: th.Tensor) -> tuple[th.Tensor, th.Tensor]:
        """
        :return: latent_policy, latent_value of the specified network.
            If all layers are shared, then ``latent_policy == latent_value``
        """
        return self.forward_actor(features), self.forward_critic(features)

    def forward_actor(self, features: th.Tensor) -> th.Tensor:
        return self.policy_net(features)

    def forward_critic(self, features: th.Tensor) -> th.Tensor:
        return self.value_net(features)


class CombinedExtractor(BaseFeaturesExtractor):
    """
    Combined features extractor for Dict observation spaces.
    Builds a features extractor for each key of the space. Input from each space
    is fed through a separate submodule (CNN or MLP, depending on input shape),
    the output features are concatenated and fed through additional MLP network ("combined").

    :param observation_space:
    :param cnn_output_dim: Number of features to output from each CNN submodule(s). Defaults to
        256 to avoid exploding network sizes.
    :param normalized_image: Whether to assume that the image is already normalized
        or not (this disables dtype and bounds checks): when True, it only checks that
        the space is a Box and has 3 dimensions.
        Otherwise, it checks that it has expected dtype (uint8) and bounds (values in [0, 255]).
    """

    def __init__(
        self,
        observation_space: spaces.Dict,
        cnn_output_dim: int = 256,
        normalized_image: bool = False,
    ) -> None:
        # TODO we do not know features-dim here before going over all the items, so put something there. This is dirty!
        super().__init__(observation_space, features_dim=1)

        extractors: dict[str, nn.Module] = {}

        total_concat_size = 0
        for key, subspace in observation_space.spaces.items():
            if is_image_space(subspace, normalized_image=normalized_image):
                extractors[key] = NatureCNN(subspace, features_dim=cnn_output_dim, normalized_image=normalized_image)
                total_concat_size += cnn_output_dim
            else:
                # The observation key is a vector, flatten it if needed
                extractors[key] = nn.Flatten()
                total_concat_size += get_flattened_obs_dim(subspace)

        self.extractors = nn.ModuleDict(extractors)

        # Update the features dim manually
        self._features_dim = total_concat_size

    def forward(self, observations: TensorDict) -> th.Tensor:
        encoded_tensor_list = []

        for key, extractor in self.extractors.items():
            encoded_tensor_list.append(extractor(observations[key]))
        return th.cat(encoded_tensor_list, dim=1)


def get_actor_critic_arch(net_arch: Union[list[int], dict[str, list[int]]]) -> tuple[list[int], list[int]]:
    """
    Get the actor and critic network architectures for off-policy actor-critic algorithms (SAC, TD3, DDPG).

    The ``net_arch`` parameter allows to specify the amount and size of the hidden layers,
    which can be different for the actor and the critic.
    It is assumed to be a list of ints or a dict.

    1. If it is a list, actor and critic networks will have the same architecture.
        The architecture is represented by a list of integers (of arbitrary length (zero allowed))
        each specifying the number of units per layer.
       If the number of ints is zero, the network will be linear.
    2. If it is a dict,  it should have the following structure:
       ``dict(qf=[<critic network architecture>], pi=[<actor network architecture>])``.
       where the network architecture is a list as described in 1.

    For example, to have actor and critic that share the same network architecture,
    you only need to specify ``net_arch=[256, 256]`` (here, two hidden layers of 256 units each).

    If you want a different architecture for the actor and the critic,
    then you can specify ``net_arch=dict(qf=[400, 300], pi=[64, 64])``.

    .. note::
        Compared to their on-policy counterparts, no shared layers (other than the features extractor)
        between the actor and the critic are allowed (to prevent issues with target networks).

    :param net_arch: The specification of the actor and critic networks.
        See above for details on its formatting.
    :return: The network architectures for the actor and the critic
    """
    if isinstance(net_arch, list):
        actor_arch, critic_arch = net_arch, net_arch
    else:
        assert isinstance(net_arch, dict), "Error: the net_arch can only contain be a list of ints or a dict"
        assert "pi" in net_arch, "Error: no key 'pi' was provided in net_arch for the actor network"
        assert "qf" in net_arch, "Error: no key 'qf' was provided in net_arch for the critic network"
        actor_arch, critic_arch = net_arch["pi"], net_arch["qf"]
    return actor_arch, critic_arch

class GINEncoder(nn.Module):
    """
    Graph Isomorphism Network (GIN) Encoder for node representation learning.

    Parameters
    ----------
    input_dim : int
        Number of input features per node.
    hidden_dim : int
        Number of hidden units per GIN layer and output embedding size.
    k_layers : int
        Number of GIN layers to use.

    Attributes
    ----------
    convs : nn.ModuleList
        List of GINConv layers, each implemented as a small MLP (2-layer MLP with ReLU).
    """

    def __init__(self, input_dim, hidden_dim, k_layers):
        super().__init__()
        self.convs = nn.ModuleList(
            [
                GINConv(
                    nn.Sequential(
                        nn.Linear(input_dim if i == 0 else hidden_dim, hidden_dim),
                        nn.ReLU(),
                        nn.Linear(hidden_dim, hidden_dim),
                    )
                )
                for i in range(k_layers)
            ]
        )

    def forward(self, x, edge_index):
        """
        Forward pass: applies K layers of GIN convolution to the input node features.

        Parameters
        ----------
        x : torch.Tensor
            Node features of shape (num_nodes, input_dim).
        edge_index : torch.LongTensor
            Graph connectivity in COO format, shape (2, num_edges).

        Returns
        -------
        torch.Tensor
            Node embeddings of shape (num_nodes, hidden_dim).
        """
        for conv in self.convs:
            x = conv(x, edge_index)
        return x  # node embeddings
    

class GNNFeatureExtractor(BaseFeaturesExtractor):
    """
    Extracts graph feature using a GINEncoder.
    Produces:
    - graph_emb: pooled graph representation (for value)
    - eligible_embs: (num_eligible, embed_dim) (for policy logits)
    """

    def __init__(
        self,
        observation_space,
        input_dim,
        hidden_dim,
        k_layers,
        op_node_id,
        pooling="max",
        
    ):
        super().__init__(observation_space, features_dim=hidden_dim)
        self.gnn = GINEncoder(input_dim, hidden_dim, k_layers)
        self.hidden_dim = hidden_dim
        self.pooling = pooling
        self.op_node_id = op_node_id
        self.aggregate = aggr.MeanAggregation()

    def forward(self, observations):
        # observations is a dict of np arrays (from GraphEnv)
        # Convert to tensors if needed
        device = next(self.parameters()).device

        # observations = {k: torch.from_numpy(v) for k, v in observations.items()}
        # TODO: Bacthing is (supposed) related to vec_env from sb3 -> need to handle in 
        node_feats = observations["node_feats"][0][:observations["num_nodes"]]
        edge_index = observations["edge_index"][0][:, :observations["num_edges"]]

        #node_feats = torch.from_numpy(node_feats).to(device)
        #edge_index = torch.from_numpy(edge_index).to(device)

        # node_feats = node_feats.float().to(device)
        # edge_index = edge_index.long().to(device)
        X = self.gnn(node_feats, edge_index)  # [current_node, hidden_dim]

        graph_emb = self.aggregate(X)
        
        # job_embeddings = torch.zeros((num_jobs, self.hidden_dim), device=device)
        # for j in range(num_jobs):
        #     op_idx = job_next_op[j]
        #     node_idx = self.op_node_id.get((j, op_idx), None)
        #     if node_idx is not None and op_idx < node_feats.shape[0]:
        #         job_embeddings[j] = x[node_idx]
        #     # (else: zeros, for jobs already done)

        # Graph embedding for critic: max over real nodes only
        # valid_node_mask = torch.sum(node_feats, dim=1) != 0
        # graph_emb = x[valid_node_mask].max(dim=0)[0]
        return X, graph_emb