from copy import deepcopy
from typing import Any, Callable, Optional, Tuple, Union
from warnings import warn

import torch
from pyknos.nflows.nn import nets
from torch import Tensor, nn, optim, relu
from torch.nn import MSELoss
from torch.nn.utils import clip_grad_norm_
from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler

from sbi.utils.sbiutils import standardizing_net, handle_invalid_x


class Destandardize(nn.Module):
    def __init__(self, mean: Union[Tensor, float], std: Union[Tensor, float]):
        super(Destandardize, self).__init__()
        mean, std = map(torch.as_tensor, (mean, std))
        self.mean = mean
        self.std = std
        self.register_buffer("_mean", mean)
        self.register_buffer("_std", std)

    def forward(self, tensor):
        return tensor * self.std + self.mean


def destandardizing_net(batch_t: Tensor, min_std: float = 1e-7) -> nn.Module:
    """Net that de-standardizes the output so the NN can learn the standardized target.

    Args:
        batch_t: Batched tensor from which mean and std deviation (across
            first dimension) are computed.
        min_std:  Minimum value of the standard deviation to use when z-scoring to
            avoid division by zero.

    Returns:
        Neural network module for z-scoring
    """

    is_valid_t, *_ = handle_invalid_x(batch_t, True)

    t_mean = torch.mean(batch_t[is_valid_t], dim=0)
    if len(batch_t > 1):
        t_std = torch.std(batch_t[is_valid_t], dim=0)
        t_std[t_std < min_std] = min_std
    else:
        t_std = 1
        logging.warning(
            f"""Using a one-dimensional batch will instantiate a Standardize transform 
            with (mean, std) parameters which are not representative of the data. We allow
            this behavior because you might be loading a pre-trained. If this is not the case, 
            please be sure to use a larger batch."""
        )

    return Destandardize(t_mean, t_std)


def build_input_output_layer(
    batch_theta: Tensor = None,
    batch_property: Tensor = None,
    z_score_theta: bool = True,
    z_score_property: bool = True,
    embedding_net_theta: nn.Module = nn.Identity(),
) -> nn.Module:
    r"""Builds input layer for the `ActiveSubspace` that optionally z-scores.

    The regression network used in the `ActiveSubspace` will receive batches of $\theta$s and properties.

    Args:
        batch_theta: Batch of $\theta$s, used to infer dimensionality and (optional)
            z-scoring.
        batch_property: Batch of properties, used for (optional) z-scoring.
        z_score_theta: Whether to z-score $\theta$s passing into the network.
        z_score_property: Whether to z-score properties passing into the network.
        embedding_net_theta: Optional embedding network for $\theta$s.
        unnormalize: Whether the layer should normalize the inputs (`False`) or
            un-normalize them (`True`).

    Returns:
        Input layer that optionally z-scores.
    """

    if z_score_theta:
        input_layer = nn.Sequential(standardizing_net(batch_theta), embedding_net_theta)
    else:
        input_layer = embedding_net_theta

    if z_score_property:
        output_layer = destandardizing_net(batch_property)
    else:
        output_layer = nn.Identity()

    return input_layer, output_layer


class ActiveSubspace:
    def __init__(self, posterior: Any):
        """
        Args:
            posterior: Posterior distribution obtained with `SNPE`, `SNLE`, or `SNRE`.
                Needs to have a `.sample()` method. If we want to analyse the
                sensitivity of the posterior probability, it also must have a
                `.log_prob()` method.
        """
        self._posterior = posterior
        self._regression_net = None
        self._theta = None
        self._emergent_property = None
        self._validation_log_probs = []

    def add_property(
        self,
        theta: Tensor,
        emergent_property: Tensor,
        model: Union[str, Callable] = "resnet",
        hidden_features: int = 100,
        num_blocks: int = 2,
        dropout_probability: float = 0.5,
        z_score_theta: bool = True,
        z_score_property: bool = True,
        embedding_net: nn.Module = nn.Identity(),
    ) -> "ActiveSubspace":
        r"""
        Add a property whose sensitivity is to be analysed.

        To analyse the sensitivity of an emergent property, we train a neural network
        to predict the property from the parameter set $\theta$. The hyperparameters of
        this neural network also have to be specified here.

        Args:
            theta: Parameter sets $\theta$ sampled from the posterior.
            emergent_property: Tensor containing the values of the property given each
                parameter set $\theta$.
            model: Neural network used to distinguish valid from bad samples. If it is
                a string, use a pre-configured network of the provided type (either
                mlp or resnet). Alternatively, a function that builds a custom
                neural network can be provided. The function will be called with the
                first batch of parameters (theta,), which can thus be used for shape
                inference and potentially for z-scoring. It needs to return a PyTorch
                `nn.Module` implementing the classifier.
            hidden_features: Number of hidden units of the classifier if `model` is a
                string.
            num_blocks: Number of hidden layers of the classifier if `model` is a
                string.
            dropout_probability: Dropout probability of the classifier if `model` is
                `resnet`.
            z_score_theta: Whether to z-score the parameters $\theta$ used to train the
                classifier.
            z_score_property: Whether to z-score the property used to train the
                classifier.
            embedding_net: Neural network used to encode the parameters before they are
                passed to the classifier.

        Returns:
            `ActiveSubspace` to make the call chainable.
        """
        assert emergent_property.shape == (
            theta.shape[0],
            1,
        ), "The `emergent_property` must have shape (N, 1)."

        self._theta = theta
        self._emergent_property = emergent_property

        if isinstance(model, str):
            if model == "resnet":

                def build_nn(theta):
                    classifier = nets.ResidualNet(
                        in_features=theta.shape[1],
                        out_features=1,
                        hidden_features=hidden_features,
                        context_features=None,
                        num_blocks=num_blocks,
                        activation=relu,
                        dropout_probability=dropout_probability,
                        use_batch_norm=True,
                    )
                    input_layer, output_layer = build_input_output_layer(
                        theta,
                        emergent_property,
                        z_score_theta,
                        z_score_property,
                        embedding_net,
                    )
                    classifier = nn.Sequential(input_layer, classifier, output_layer)
                    return classifier

            elif model == "mlp":

                def build_nn(theta):
                    classifier = nn.Sequential(
                        nn.Linear(theta.shape[1], hidden_features),
                        nn.BatchNorm1d(hidden_features),
                        nn.ReLU(),
                        nn.Linear(hidden_features, hidden_features),
                        nn.BatchNorm1d(hidden_features),
                        nn.ReLU(),
                        nn.Linear(hidden_features, 1),
                    )
                    input_layer, output_layer = build_input_output_layer(
                        theta,
                        emergent_property,
                        z_score_theta,
                        z_score_property,
                        embedding_net,
                    )
                    classifier = nn.Sequential(input_layer, classifier, output_layer)
                    return classifier

            else:
                raise NameError
        else:
            build_nn = model

        self._build_nn = build_nn

        return self

    def train(
        self,
        training_batch_size: int = 50,
        learning_rate: float = 5e-4,
        validation_fraction: float = 0.1,
        stop_after_epochs: int = 20,
        max_num_epochs: Optional[int] = None,
        clip_max_norm: Optional[float] = 5.0,
    ) -> nn.Module:
        r"""
        Train a regression network to predict the specified property from $\theta$.

        Args:
            training_batch_size: Training batch size.
            learning_rate: Learning rate for Adam optimizer.
            validation_fraction: The fraction of data to use for validation.
            stop_after_epochs: The number of epochs to wait for improvement on the
                validation set before terminating training.
            max_num_epochs: Maximum number of epochs to run. If reached, we stop
                training even when the validation loss is still decreasing. If None, we
                train until validation loss increases (see also `stop_after_epochs`).
            clip_max_norm: Value at which to clip the total gradient norm in order to
                prevent exploding gradients. Use None for no clipping.
                good_bad_criterion: Should take in the simulation output $x$ and output
                whether $x$ is counted as `valid` simulation (output 1.0) or as a `bad`
                simulation output 0.0). By default, the function checks whether $x$
                contains at least one `nan` or `inf`.
        """

        # Get indices for permutation of the data.
        num_examples = len(self._theta)
        permuted_indices = torch.randperm(num_examples)
        num_training_examples = int((1 - validation_fraction) * num_examples)
        num_validation_examples = num_examples - num_training_examples
        train_indices, val_indices = (
            permuted_indices[:num_training_examples],
            permuted_indices[num_training_examples:],
        )

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(self._theta, self._emergent_property)

        # Create neural_net and validation loaders using a subset sampler.
        train_loader = data.DataLoader(
            dataset,
            batch_size=training_batch_size,
            drop_last=True,
            sampler=SubsetRandomSampler(train_indices),
        )
        val_loader = data.DataLoader(
            dataset,
            batch_size=min(training_batch_size, num_examples - num_training_examples),
            shuffle=False,
            drop_last=True,
            sampler=SubsetRandomSampler(val_indices),
        )

        if self._regression_net is None:
            self._regression_net = self._build_nn(self._theta[train_indices])

        optimizer = optim.Adam(
            list(self._regression_net.parameters()),
            lr=learning_rate,
        )
        max_num_epochs = 2 ** 31 - 1 if max_num_epochs is None else max_num_epochs

        # criterion / loss
        criterion = MSELoss()

        epoch, self._val_log_prob = 0, float("-Inf")
        while epoch <= max_num_epochs and not self._converged(epoch, stop_after_epochs):
            self._regression_net.train()
            for parameters, observations in train_loader:
                optimizer.zero_grad()
                outputs = self._regression_net(parameters)
                loss = criterion(outputs, observations)
                loss.backward()
                if clip_max_norm is not None:
                    clip_grad_norm_(
                        self._regression_net.parameters(),
                        max_norm=clip_max_norm,
                    )
                optimizer.step()

            epoch += 1

            # calculate validation performance
            self._regression_net.eval()

            val_loss = 0.0
            with torch.no_grad():
                for parameters, observations in val_loader:
                    outputs = self._regression_net(parameters)
                    loss = criterion(outputs, observations)
                    val_loss += loss.item()
            self._val_log_prob = -val_loss / num_validation_examples
            self._validation_log_probs.append(self._val_log_prob)

            print("Training neural network. Epochs trained: ", epoch, end="\r")

        return deepcopy(self._regression_net)

    def _converged(self, epoch: int, stop_after_epochs: int) -> bool:
        r"""
        Return whether the training converged yet and save best model state so far.
        Checks for improvement in validation performance over previous epochs.
        Args:
            epoch: Current epoch in training.
            stop_after_epochs: How many fruitless epochs to let pass before stopping.
        Returns:
            Whether the training has stopped improving, i.e. has converged.
        """
        converged = False

        posterior_nn = self._regression_net

        # (Re)-start the epoch count with the first epoch or any improvement.
        if epoch == 0 or self._val_log_prob > self._best_val_log_prob:
            self._best_val_log_prob = self._val_log_prob
            self._epochs_since_last_improvement = 0
            self._best_model_state_dict = deepcopy(posterior_nn.state_dict())
        else:
            self._epochs_since_last_improvement += 1

        # If no validation improvement over many epochs, stop training.
        if self._epochs_since_last_improvement > stop_after_epochs - 1:
            posterior_nn.load_state_dict(self._best_model_state_dict)
            converged = True

        return converged

    def find_active(
        self,
        posterior_log_prob_as_property: bool = False,
        norm_gradients_to_prior: bool = True,
        num_monte_carlo_samples: int = 1000,
    ) -> Tuple[Tensor, Tensor]:
        """
        Return eigenvectors and values corresponding to directions of sensitivity.

        The directions of sensitivity are the directions along which a specific
        property changes in the fastest way.

        This computes:
        $M = E_p(\theta|x)[\nabla f(\theta)^T \nabla f(\theta)]$
        where f(\cdot) is the trained regression network. The expected value is
        approximated with a Monte-Carlo mean. Next, do an eigenvalue
        decomposition of the matrix $M$:
        $M = Q \Lambda Q^{-1}$

        Args:
            posterior_log_prob_as_property: Whether to use the posterior
                log-probability the key property whose sensitivity is analysed. If
                `False`, one must have specified an emergent property and trained a
                regression network using `.add_property().train()`. If `True`,
                any previously specified property is ignored.
            norm_gradients_to_prior: Whether to normalize each entry of the gradient
                by the standard deviation of the prior in each dimension. If set to
                `False`, the directions with the strongest eigenvalues might correspond
                to directions in which the prior is broad.
            num_monte_carlo_samples: Number of Monte Carlo samples that the average is
                based on. A larger value will make the results more accurate while
                requiring more compute time.

        Returns: Eigenvectors and corresponding eigenvalues. They are sorted in
            ascending order.
        """

        self._gradients_are_normed = norm_gradients_to_prior

        if self._emergent_property is None and not posterior_log_prob_as_property:
            raise ValueError(
                "You have not yet passed an emergent property whose "
                "sensitivity you would like to analyse. Please use "
                "`.add_emergent_property().train()` to do so. If you want "
                "to use all features that had also been used to infer the "
                "posterior distribution (i.e. you want to analyse the "
                "sensitivity of the posterior probability), use: "
                "`.find_active(posterior_log_prob_as_property=True)`."
            )
        if self._emergent_property is not None and posterior_log_prob_as_property:
            warn(
                "You specified a property with `.add_property()`, but also set "
                "`posterior_log_prob_as_property=True`. The specified property will "
                "be ignored."
            )

        thetas = self._posterior.sample((num_monte_carlo_samples,))

        thetas.requires_grad = True

        if posterior_log_prob_as_property:
            predictions = self._posterior.log_prob(thetas, track_gradients=True)
        else:
            predictions = self._regression_net.forward(thetas)
        loss = predictions.mean()
        loss.backward()
        gradients = torch.squeeze(thetas.grad)
        if norm_gradients_to_prior:
            if hasattr(self._posterior._prior, "stddev") and hasattr(
                self._posterior._prior, "mean"
            ):
                self._prior_mean = self._posterior._prior.mean
                self._prior_scale = self._posterior._prior.stddev
            else:
                prior_samples = self._posterior._prior.sample((10000,))
                self._prior_scale = torch.std(prior_samples, dim=0)
                self._prior_mean = torch.mean(prior_samples, dim=0)
            gradients *= self._prior_scale
        outer_products = torch.einsum("bi,bj->bij", (gradients, gradients))
        average_outer_product = outer_products.mean(dim=0)

        eigen_values, eigen_vectors = torch.symeig(
            average_outer_product, eigenvectors=True
        )

        self._eigen_vectors = eigen_vectors

        return eigen_values, eigen_vectors

    def project(self, theta: Tensor, num_dimensions: int) -> Tensor:
        r"""
        Return $theta$ that were projected into the subspace.

        Args:
            theta: Parameter sets to be projected.
            num_dimensions: Dimensionality of the subspace into which to project.

        Returns:
            Projected parameters of shape (theta.shape[0], num_dimensions).
        """
        if self._gradients_are_normed:
            theta = (theta - self._prior_mean) / self._prior_scale

        projection_mat = self._eigen_vectors[:, -num_dimensions:]
        projected_theta = torch.mm(theta, projection_mat)

        return projected_theta
