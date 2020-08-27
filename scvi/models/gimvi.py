import numpy as np
import logging
import torch
from anndata import AnnData

from typing import List, Optional
from scvi.core.models import JVAE, Classifier
from scvi.core.trainers.jvae_trainer import JPosterior
from scvi.core.trainers import JVAETrainer
from scvi.models._base import VAEMixin, BaseModelClass
from scvi import _CONSTANTS
from scvi.models._utils import _get_var_names_from_setup_anndata

logger = logging.getLogger(__name__)


class GIMVI(VAEMixin, BaseModelClass):
    """
    Joint VAE for imputing missing genes in spatial data [Lopez19]_

    Parameters
    ----------
    adata_seq
        AnnData object that has been registered with scvi and contains RNA-seq data
    adata_spatial
        AnnData object that has been registered with scvi and contains spatial data
    n_hidden
        Number of nodes per hidden layer
    generative_distributions
        List of generative distribution for adata_seq data and adata_spatial data
    model_library_size
        List of bool of whether to model library size for adata_seq and adata_spatial
    n_latent
        Dimensionality of the latent space

    Examples
    --------

    >>> adata_seq = anndata.read_h5ad(path_to_anndata_seq)
    >>> adata_spatial = anndata.read_h5ad(path_to_anndata_spatial)
    >>> scvi.dataset.setup_anndata(adata_seq)
    >>> scvi.dataset.setup_anndata(adata_spatial)
    >>> vae = scvi.models.GIMVI(adata_seq, adata_spatial)
    >>> vae.train(n_epochs=400)
    """

    def __init__(
        self,
        adata_seq: AnnData,
        adata_spatial: AnnData,
        generative_distributions: List = ["zinb", "nb"],
        model_library_size: List = [True, False],
        n_latent: int = 10,
        use_cuda: bool = True,
        **model_kwargs,
    ):
        super(GIMVI, self).__init__(use_cuda=use_cuda)
        self.use_cuda = use_cuda and torch.cuda.is_available()
        self.adatas = [adata_seq, adata_spatial]
        seq_var_names = _get_var_names_from_setup_anndata(adata_seq)
        spatial_var_names = _get_var_names_from_setup_anndata(adata_spatial)
        spatial_gene_loc = [
            np.argwhere(seq_var_names == g)[0] for g in spatial_var_names
        ]
        spatial_gene_loc = np.concatenate(spatial_gene_loc)
        gene_mappings = [slice(None), spatial_gene_loc]
        sum_stats = [d.uns["_scvi"]["summary_stats"] for d in self.adatas]
        n_inputs = [s["n_genes"] for s in sum_stats]
        total_genes = adata_seq.uns["_scvi"]["summary_stats"]["n_genes"]
        n_batches = sum([s["n_batch"] for s in sum_stats])

        self.model = JVAE(
            n_inputs,
            total_genes,
            gene_mappings,
            generative_distributions,
            model_library_size,
            n_batch=n_batches,
            n_latent=n_latent,
            **model_kwargs,
        )

        self._model_summary_string = "gimVI model with params"

    @property
    def _trainer_class(self):
        return JVAETrainer

    @property
    def _posterior_class(self):
        return JPosterior

    def train(
        self,
        n_epochs: Optional[int] = 200,
        kappa: Optional[int] = 5,
        discriminator: Optional[Classifier] = None,
    ):

        discriminator = Classifier(self.model.n_latent, 32, 2, 3, logits=True)
        self.trainer = JVAETrainer(
            self.model, discriminator, self.adatas, 0.95, frequency=1, kappa=kappa
        )
        self.trainer.train(n_epochs=n_epochs)

        self.is_trained = True

    def _make_posteriors(self, adatas: List[AnnData] = None, batch_size=128):
        if adatas is None:
            adatas = self.adatas
        post_list = [
            self._make_posterior(adata, mode=i) for i, adata in enumerate(adatas)
        ]

        return post_list

    def get_latent_representation(
        self, adatas: List[AnnData] = None, deterministic: bool = True, batch_size=128
    ) -> List[np.ndarray]:
        """Return the latent space embedding for each dataset

        Parameters
        ----------
        deterministic
            If true, use the mean of the encoder instead of a Gaussian sample
        """
        if adatas is None:
            adatas = self.adatas
        posteriors = self._make_posteriors(adatas, batch_size=batch_size)
        self.model.eval()
        latents = []
        for mode, posterior in enumerate(posteriors):
            latent = []
            for tensors in posterior:
                (
                    sample_batch,
                    local_l_mean,
                    local_l_var,
                    batch_index,
                    label,
                    *_,
                ) = self._unpack_tensors(tensors)
                latent.append(
                    self.model.sample_from_posterior_z(
                        sample_batch, mode, deterministic=deterministic
                    )
                )

            latent = torch.cat(latent).cpu().detach().numpy()
            latents.append(latent)

        return latents

    def _unpack_tensors(self, tensors):
        x = tensors[_CONSTANTS.X_KEY].squeeze_(0)
        local_l_mean = tensors[_CONSTANTS.LOCAL_L_MEAN_KEY].squeeze_(0)
        local_l_var = tensors[_CONSTANTS.LOCAL_L_VAR_KEY].squeeze_(0)
        batch_index = tensors[_CONSTANTS.BATCH_KEY].squeeze_(0)
        y = tensors[_CONSTANTS.LABELS_KEY].squeeze_(0)
        return x, local_l_mean, local_l_var, batch_index, y

    def get_imputed_values(
        self,
        adatas: List[AnnData] = None,
        deterministic: bool = True,
        normalized: bool = True,
        decode_mode: Optional[int] = None,
        batch_size: Optional[int] = 128,
    ) -> List[np.ndarray]:
        """Return imputed values for all genes for each dataset

        Parameters
        ----------
        deterministic
            If true, use the mean of the encoder instead of a Gaussian sample for the latent vector
        normalized
            Return imputed normalized values or not
        decode_mode
            If a `decode_mode` is given, use the encoder specific to each dataset as usual but use
            the decoder of the dataset of id `decode_mode` to impute values
        """
        self.model.eval()

        if adatas is None:
            adatas = self.adatas
        posteriors = self._make_posteriors(adatas, batch_size=batch_size)

        imputed_values = []
        for mode, posterior in enumerate(posteriors):
            imputed_value = []
            for tensors in posterior:
                (
                    sample_batch,
                    local_l_mean,
                    local_l_var,
                    batch_index,
                    label,
                    *_,
                ) = self._unpack_tensors(tensors)
                if normalized:
                    imputed_value.append(
                        self.model.sample_scale(
                            sample_batch,
                            mode,
                            batch_index,
                            label,
                            deterministic=deterministic,
                            decode_mode=decode_mode,
                        )
                    )
                else:
                    imputed_value.append(
                        self.model.sample_rate(
                            sample_batch,
                            mode,
                            batch_index,
                            label,
                            deterministic=deterministic,
                            decode_mode=decode_mode,
                        )
                    )

            imputed_value = torch.cat(imputed_value).cpu().detach().numpy()
            imputed_values.append(imputed_value)

        return imputed_values
