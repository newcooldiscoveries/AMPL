#!/usr/bin/env python

"""
Contains class ModelWrapper and its subclasses, which are wrappers for DeepChem and scikit-learn model classes.
"""

import logging
import os
import shutil
import joblib
import pdb

import deepchem as dc
import numpy as np
import tensorflow as tf
if dc.__version__.startswith('2.1'):
    from deepchem.models.tensorgraph.fcnet import MultitaskRegressor, MultitaskClassifier
else:
    from deepchem.models import MultitaskRegressor, MultitaskClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.ensemble import RandomForestRegressor

try:
    import xgboost as xgb
    xgboost_supported = True
except ImportError:
    xgboost_supported = False

import pickle
import yaml
import glob
from datetime import datetime
import time

from atomsci.ddm.utils import datastore_functions as dsf
from atomsci.ddm.utils import llnl_utils
from atomsci.ddm.pipeline import transformations as trans
from atomsci.ddm.pipeline import perf_data as perf

logging.basicConfig(format='%(asctime)-15s %(message)s')


# ****************************************************************************************
def create_model_wrapper(params, featurizer, ds_client=None):
    """Factory function for creating Model objects of the correct subclass for params.model_type.

    Args:
        params (Namespace) : Parameters passed to the model pipeline
        featurizer (Featurization): Object managing the featurization of compounds
        ds_client (DatastoreClient): Interface to the file datastore

    Returns:
        model (pipeline.Model): Wrapper for DeepChem, sklearn or other model.

    Raises:
        ValueError: Only params.model_type = 'NN', 'RF' or 'xgboost' is supported.
    """
    if params.model_type == 'NN':
        return DCNNModelWrapper(params, featurizer, ds_client)
    elif params.model_type == 'RF':
        return DCRFModelWrapper(params, featurizer, ds_client)
    elif params.model_type == 'xgboost':
        if not xgboost_supported:
            raise Exception("Unable to import xgboost. \
                             xgboost package needs to be installed to use xgboost model. \
                             Installatin: \
                             from pip: pip3 install xgboost==0.90.\
                             livermore compute (lc): /usr/mic/bio/anaconda3/bin/pip install xgboost==0.90 --user \
                             twintron-blue (TTB): /opt/conda/bin/pip install xgboost==0.90 --user/ \ "
                            )
        elif float(xgb.__version__) < 0.9:
            raise Exception(f"xgboost required to be >= 0.9 for GPU support. \
                             current version = {float(xgb.__version__)} \
                             installatin: \
                             from pip: pip3 install xgboost==0.90 \
                             livermore compute (lc): /usr/mic/bio/anaconda3/bin/pip install xgboost==0.90 --user \
                             twintron-blue (TTB): /opt/conda/bin/pip install xgboost==0.90 --user/ "
                            )
        else:
            return DCxgboostModelWrapper(params, featurizer, ds_client)
    else:
        raise ValueError("Unknown model_type %s" % params.model_type)

# ****************************************************************************************

class ModelWrapper(object):
    """Wrapper for DeepChem and sklearn model objects. Provides methods to train and test a model,
    generate predictions for an input dataset, and generate performance metrics for these predictions.

        Attributes:
        Set in __init__
            params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
            featurziation (Featurization object): The featurization object created outside of model_wrapper
            log (log): The logger
            output_dir (str): The parent path of the model directory
            transformers (list): Initialized as an empty list, stores the transformers on the response col
            transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers

        set in setup_model_dirs:
            best_model_dir (str): The subdirectory under output_dir that contains the best model. Created in setup_model_dirs

    """
    def __init__(self, params, featurizer, ds_client):
        """Initializes ModelWrapper object.

        Args:
            params (Namespace object): contains all parameter information.
            featurizer (Featurization object): initialized outside of model_wrapper
            ds_client (DatastoreClient): Interface to the file datastore

        Side effects:
            Sets the following attributes of ModelWrapper:
                params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
                featurziation (Featurization object): The featurization object created outside of model_wrapper
                log (log): The logger
                output_dir (str): The parent path of the model directory
                transformers (list): Initialized as an empty list, stores the transformers on the response col
                transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers

        """
        self.params = params
        self.featurization = featurizer
        self.ds_client = ds_client
        self.log = logging.getLogger('ATOM')
        self.output_dir = self.params.output_dir
        self.model_dir = os.path.join(self.output_dir, 'model')
        os.makedirs(self.model_dir, exist_ok=True)
        self.transformers = []
        self.transformers_x = []

        # ****************************************************************************************

    def setup_model_dirs(self):
        """Sets up paths and directories for persisting models at particular training epochs, used by
        the DeepChem model classes.

        Side effects:
            Sets the following attributes of ModelWrapper:
                best_model_dir (str): The subdirectory under output_dir that contains the best model. Created in setup_model_dirs
        """
        self.best_model_dir = os.path.join(self.output_dir, 'best_model')

        # ****************************************************************************************

    def train(self, pipeline):
        """Trains a model (for multiple epochs if applicable), and saves the tuned model.

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Raises:
            NotImplementedError: The method is implemented by subclasses
        """
        raise NotImplementedError

        # ****************************************************************************************

    def get_model_specific_metadata(self):
        """Returns a dictionary of parameter settings for this ModelWrapper object that are specific
        to the model type.

        Raises:
            NotImplementedError: The method is implemented by subclasses
        """
        raise NotImplementedError
        # ****************************************************************************************

    def create_transformers(self, model_dataset):
        """
        Initialize transformers for responses and/or features, and persist them for later.

        Args:
            model_dataset: The ModelDataset object that handles the current dataset

        Side effects
            Overwrites the attributes:
                transformers: A list of deepchem transformation objects on response_col, only if conditions are met
                transformers_x: A list of deepchem transformation objects on featurizers, only if conditions are met.
                params.transformer_key: A string pointing to the dataset key containing the transformer in the datastore, or the path to the transformer

        """
        # TODO: Just a warning, we may have response transformers for classification datasets in the future
        if self.params.prediction_type=='regression' and self.params.transformers==True:
            # self.transformers = [
            #    dc.trans.NormalizationTransformer(transform_y=True, dataset=model_dataset.dataset)]
            self.transformers = [ 
                trans.NormalizationTransformerMissingData(transform_y=True, dataset=model_dataset.dataset)
            ]

        # Set up transformers for features, if needed
        self.transformers_x = trans.create_feature_transformers(self.params, model_dataset)

        if len(self.transformers) > 0 or len(self.transformers_x) > 0:

            if self.params.save_results:
                # Save tuple of response and feature transformer lists in datastore
                self.params.transformer_key = 'transformers_' + self.params.model_uuid + '.pkl'
                try:
                    fileupload = dsf.upload_pickle_to_DS(data = (self.transformers, self.transformers_x),
                                    bucket = self.params.transformer_bucket,
                                    filename = os.path.basename(self.params.transformer_key),
                                    title = "Saved transformers for dataset %s" % model_dataset.dataset_name,
                                    description = "Saved transformers for dataset %s" % model_dataset.dataset_name,
                                    tags = ['transformers', 'pickled', self.params.featurizer.lower(), model_dataset.dataset_name],
                                    key_values = {'file_category': 'transformers'},
                                    client = self.ds_client,
                                    dataset_key = self.params.transformer_key,
                                    override_check = True,
                                    return_metadata=True)
                    self.params.transformer_oid = fileupload['dataset_oid']

                except Exception as e:
                    self.log.warning("Error when trying to save transformers to datastore:\n%s" % str(e))
            else:
                self.params.transformer_key = os.path.join(self.output_dir, 'transformers.pkl')
                pickle.dump((self.transformers, self.transformers_x), open(self.params.transformer_key, 'wb'))
                self.log.info("Wrote transformers to %s" % self.params.transformer_key)
                self.params.transformer_bucket = self.params.bucket

        # ****************************************************************************************

    def transform_dataset(self, dataset):
        """
        Transform the responses and/or features in the given DeepChem dataset using the current transformers.

        Args:
            dataset: The DeepChem DiskDataset that contains a dataset

        Returns:
            transformed_dataset: The transformed DeepChem DiskDataset

        """
        transformed_dataset = dataset
        if len(self.transformers) > 0:
            self.log.info("Transforming response data")
            for transformer in self.transformers:
                transformed_dataset = transformer.transform(transformed_dataset)
        if len(self.transformers_x) > 0:
            self.log.info("Transforming feature data")
            for transformer in self.transformers_x:
                transformed_dataset = transformer.transform(transformed_dataset)

        return transformed_dataset
        # ****************************************************************************************

    def get_num_features(self):
        """Returns the number of dimensions of the feature space, taking both featurization method
        and transformers into account.
        """
        if self.params.feature_transform_type == 'umap':
            return self.params.umap_dim
        else:
            return self.featurization.get_feature_count()

        # ****************************************************************************************

    def get_train_valid_pred_results(self, perf_data):
        """Returns predicted values and metrics for the training, validation or test set
        associated with the PerfData object perf_data. Results are returned as a dictionary 
        of parameter, value pairs in the format expected by the model tracker.

        Args:
            perf_data: A PerfData object that stores the predicted values and metrics
        Returns:
            dict: A dictionary of the prediction results

        """
        return perf_data.get_prediction_results()

        # ****************************************************************************************
    def get_test_perf_data(self, model_dir, model_dataset):
        """Returns the predicted values and metrics for the current test dataset against
        the version of the model stored in model_dir, as a PerfData object.

        Args:
            model_dir (str): Directory where the saved model is stored
            model_dataset (DiskDataset): Stores the current dataset and related methods

        Returns:
            perf_data: PerfData object containing the predicted values and metrics for the current test dataset
        """
        # Load the saved model from model_dir
        self.reload_model(model_dir)

        # Create a PerfData object, which knows how to format the prediction results in the structure
        # expected by the model tracker.

        # We pass transformed=False to indicate that the preds and uncertainties we get from
        # generate_predictions are already untransformed, so that perf_data.get_prediction_results()
        # doesn't untransform them again.
        perf_data = perf.create_perf_data(self.params.prediction_type, model_dataset, self.transformers, 'test', transformed=False)
        test_dset = model_dataset.test_dset
        test_preds, test_stds = self.generate_predictions(test_dset)
        _ = perf_data.accumulate_preds(test_preds, test_dset.ids, test_stds)
        return perf_data

        # ****************************************************************************************
    def get_test_pred_results(self, model_dir, model_dataset):
        """Returns predicted values and metrics for the current test dataset against the version
        of the model stored in model_dir, as a dictionary in the format expected by the model tracker.

        Args:
            model_dir (str): Directory where the saved model is stored
            model_dataset (DiskDataset): Stores the current dataset and related methods

        Returns:
            dict: A dictionary containing the prediction values and metrics for the current dataset.
        """
        perf_data = self.get_test_perf_data(model_dir, model_dataset)
        return perf_data.get_prediction_results()

        # ****************************************************************************************
    def get_full_dataset_perf_data(self, model_dataset):
        """Returns the predicted values and metrics from the current model for the full current dataset,
        as a PerfData object.

        Args:
            model_dataset (DiskDataset): Stores the current dataset and related methods

        Returns:
            perf_data: PerfData object containing the predicted values and metrics for the current full dataset
        """

        # Create a PerfData object, which knows how to format the prediction results in the structure
        # expected by the model tracker.

        # We pass transformed=False to indicate that the preds and uncertainties we get from
        # generate_predictions are already untransformed, so that perf_data.get_prediction_results()
        # doesn't untransform them again.
        perf_data = perf.create_perf_data(self.params.prediction_type, model_dataset, self.transformers, 'full', transformed=False)
        full_preds, full_stds = self.generate_predictions(model_dataset.dataset)
        _ = perf_data.accumulate_preds(full_preds, model_dataset.dataset.ids, full_stds)
        return perf_data

        # ****************************************************************************************
    def get_full_dataset_pred_results(self, model_dataset):
        """Returns predicted values and metrics from the current model for the full current dataset,
        as a dictionary in the format expected by the model tracker.

        Args:
            model_dataset (DiskDataset): Stores the current dataset and related methods
        Returns:
            dict: A dictionary containing predicted values and metrics for the current full dataset

        """
        self.data = model_dataset
        perf_data = self.get_full_dataset_perf_data(model_dataset)
        return perf_data.get_prediction_results()

    def generate_predictions(self, dataset):
        """

        Args:
            dataset:

        Returns:

        """
        raise NotImplementedError

    def reload_model(self, reload_dir):
        """

        Args:
            reload_dir:

        Returns:

        """
        raise NotImplementedError


# ****************************************************************************************
class DCNNModelWrapper(ModelWrapper):
    """Contains methods to load in a dataset, split and featurize the data, fit a model to the train dataset,
    generate predictions for an input dataset, and generate performance metrics for these predictions.

    Attributes:
        Set in __init__
            params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
            featurziation (Featurization object): The featurization object created outside of model_wrapper
            log (log): The logger
            output_dir (str): The parent path of the model directory
            transformers (list): Initialized as an empty list, stores the transformers on the response col
            transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers
            model_dir (str): The subdirectory under output_dir that contains the model. Created in setup_model_dirs.
            best_model_dir (str): The subdirectory under output_dir that contains the best model. Created in setup_model_dirs
            g: The tensorflow graph object
            sess: The tensor flow graph session
            model: The dc.models.GraphConvModel, MultitaskRegressor, or MultitaskClassifier object, as specified by the params attribute

        Created in train:
            data (ModelDataset): contains the dataset, set in pipeline
            best_epoch (int): Initialized as None, keeps track of the epoch with the best validation score
            train_perf_data (np.array of PerfData): Initialized as an empty array, 
                contains the predictions and performance of the training dataset
            valid_perf_data (np.array of PerfData): Initialized as an empty array,
                contains the predictions and performance of the validation dataset
            train_epoch_perfs (np.array of dicts): Initialized as an empty array,
                contains a list of dictionaries of predicted values and metrics on the training dataset
            valid_epoch_perfs (np.array of dicts): Initialized as an empty array,
                contains a list of dictionaries of predicted values and metrics on the validation dataset

    """

    def __init__(self, params, featurizer, ds_client):
        """Initializes DCNNModelWrapper object.

        Args:
            params (Namespace object): contains all parameter information.
            featurizer (Featurizer object): initialized outside of model_wrapper

        Side effects:
            params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
            featurziation (Featurization object): The featurization object created outside of model_wrapper
            log (log): The logger
            output_dir (str): The parent path of the model directory
            transformers (list): Initialized as an empty list, stores the transformers on the response col
            transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers
            g: The tensorflow graph object
            sess: The tensor flow graph session
            model: The dc.models.GraphConvModel, MultitaskRegressor, or MultitaskClassifier object, as specified by the params attribute


        """
        super().__init__(params, featurizer, ds_client)
        self.g = tf.Graph()
        self.sess = tf.Session(graph=self.g)
        n_features = self.get_num_features()
        self.num_epochs_trained = 0

        if self.params.featurizer == 'graphconv':

            # Set defaults for layer sizes and dropouts, if not specified by caller. Note that
            # these depend on the featurizer used.

            if self.params.layer_sizes is None:
                self.params.layer_sizes = [64, 64, 128]
            if self.params.dropouts is None:
                if self.params.uncertainty:
                    self.params.dropouts = [0.25] * len(self.params.layer_sizes)
                else:
                    self.params.dropouts = [0.0] * len(self.params.layer_sizes)

            # TODO: Need to check that GraphConvModel params are actually being used
            self.model = dc.models.GraphConvModel(
                self.params.num_model_tasks,
                batch_size=self.params.batch_size,
                learning_rate=self.params.learning_rate,
                learning_rate_decay_time=1000,
                optimizer_type=self.params.optimizer_type,
                beta1=0.9,
                beta2=0.999,
                model_dir=self.model_dir,
                mode=self.params.prediction_type,
                tensorboard=False,
                uncertainty=self.params.uncertainty,
                graph_conv_layers=self.params.layer_sizes[:-1],
                dense_layer_size=self.params.layer_sizes[-1],
                dropout=self.params.dropouts,
                penalty=self.params.weight_decay_penalty,
                penalty_type=self.params.weight_decay_penalty_type)

        else:
            # Set defaults for layer sizes and dropouts, if not specified by caller. Note that
            # default layer sizes depend on the featurizer used.

            if self.params.layer_sizes is None:
                if self.params.featurizer == 'ecfp':
                    self.params.layer_sizes = [1000, 500]
                elif self.params.featurizer == 'descriptors':
                    self.params.layer_sizes = [200, 100]
                else:
                    # Shouldn't happen
                    self.log.warning("You need to define default layer sizes for featurizer %s" %
                                     self.params.featurizer)
                    self.params.layer_sizes = [1000, 500]

            if self.params.dropouts is None:
                self.params.dropouts = [0.4] * len(self.params.layer_sizes)
            if self.params.weight_init_stddevs is None:
                self.params.weight_init_stddevs = [0.02] * len(self.params.layer_sizes)
            if self.params.bias_init_consts is None:
                self.params.bias_init_consts = [1.0] * len(self.params.layer_sizes)

            if self.params.prediction_type == 'regression':

                # TODO: Need to check that MultitaskRegressor params are actually being used
                self.model = MultitaskRegressor(
                    self.params.num_model_tasks,
                    n_features,
                    layer_sizes=self.params.layer_sizes,
                    dropouts=self.params.dropouts,
                    weight_init_stddevs=self.params.weight_init_stddevs,
                    bias_init_consts=self.params.bias_init_consts,
                    learning_rate=self.params.learning_rate,
                    weight_decay_penalty=self.params.weight_decay_penalty,
                    weight_decay_penalty_type=self.params.weight_decay_penalty_type,
                    batch_size=self.params.batch_size,
                    seed=123,
                    verbosity='low',
                    model_dir=self.model_dir,
                    learning_rate_decay_time=1000,
                    beta1=0.9,
                    beta2=0.999,
                    mode=self.params.prediction_type,
                    tensorboard=False,
                    uncertainty=self.params.uncertainty)

                # print("JEA debug",self.params.num_model_tasks,n_features,self.params.layer_sizes,self.params.weight_init_stddevs,self.params.bias_init_consts,self.params.dropouts,self.params.weight_decay_penalty,self.params.weight_decay_penalty_type,self.params.batch_size,self.params.learning_rate)
                # self.model = MultitaskRegressor(
                #     self.params.num_model_tasks,
                #     n_features,
                #     layer_sizes=self.params.layer_sizes,
                #     weight_init_stddevs=self.params.weight_init_stddevs,
                #     bias_init_consts=self.params.bias_init_consts,
                #     dropouts=self.params.dropouts,
                #     weight_decay_penalty=self.params.weight_decay_penalty,
                #     weight_decay_penalty_type=self.params.weight_decay_penalty_type,
                #     batch_size=self.params.batch_size,
                #     learning_rate=self.params.learning_rate,
                #     seed=123)

            else:
                # TODO: Need to check that MultitaskClassifier params are actually being used
                self.model = MultitaskClassifier(
                    self.params.num_model_tasks,
                    n_features,
                    layer_sizes=self.params.layer_sizes,
                    dropouts=self.params.dropouts,
                    weight_init_stddevs=self.params.weight_init_stddevs,
                    bias_init_consts=self.params.bias_init_consts,
                    learning_rate=self.params.learning_rate,
                    weight_decay_penalty=self.params.weight_decay_penalty,
                    weight_decay_penalty_type=self.params.weight_decay_penalty_type,
                    batch_size=self.params.batch_size,
                    seed=123,
                    verbosity='low',
                    model_dir=self.model_dir,
                    learning_rate_decay_time=1000,
                    beta1=.9,
                    beta2=.999,
                    mode=self.params.prediction_type,
                    tensorboard=False,
                    n_classes=self.params.class_number)

    # ****************************************************************************************
    def recreate_model(self):
        """
        Creates a new DeepChem Model object of the correct type for the requested featurizer and prediction type 
        and returns it.
        """
        if self.params.featurizer == 'graphconv':
            model = dc.models.GraphConvModel(
                self.params.num_model_tasks,
                batch_size=self.params.batch_size,
                learning_rate=self.params.learning_rate,
                learning_rate_decay_time=1000,
                optimizer_type=self.params.optimizer_type,
                beta1=0.9,
                beta2=0.999,
                model_dir=self.model_dir,
                mode=self.params.prediction_type,
                tensorboard=False,
                uncertainty=self.params.uncertainty,
                graph_conv_layers=self.params.layer_sizes[:-1],
                dense_layer_size=self.params.layer_sizes[-1],
                dropout=self.params.dropouts,
                penalty=self.params.weight_decay_penalty,
                penalty_type=self.params.weight_decay_penalty_type)

        else:
            n_features = self.get_num_features()
            if self.params.prediction_type == 'regression':
                model = MultitaskRegressor(
                    self.params.num_model_tasks,
                    n_features,
                    layer_sizes=self.params.layer_sizes,
                    dropouts=self.params.dropouts,
                    weight_init_stddevs=self.params.weight_init_stddevs,
                    bias_init_consts=self.params.bias_init_consts,
                    learning_rate=self.params.learning_rate,
                    weight_decay_penalty=self.params.weight_decay_penalty,
                    weight_decay_penalty_type=self.params.weight_decay_penalty_type,
                    batch_size=self.params.batch_size,
                    seed=123,
                    verbosity='low',
                    model_dir=self.model_dir,
                    learning_rate_decay_time=1000,
                    beta1=0.9,
                    beta2=0.999,
                    mode=self.params.prediction_type,
                    tensorboard=False,
                    uncertainty=self.params.uncertainty)
            else:
                model = MultitaskClassifier(
                    self.params.num_model_tasks,
                    n_features,
                    layer_sizes=self.params.layer_sizes,
                    dropouts=self.params.dropouts,
                    weight_init_stddevs=self.params.weight_init_stddevs,
                    bias_init_consts=self.params.bias_init_consts,
                    learning_rate=self.params.learning_rate,
                    weight_decay_penalty=self.params.weight_decay_penalty,
                    weight_decay_penalty_type=self.params.weight_decay_penalty_type,
                    batch_size=self.params.batch_size,
                    seed=123,
                    verbosity='low',
                    model_dir=self.model_dir,
                    learning_rate_decay_time=1000,
                    beta1=.9,
                    beta2=.999,
                    mode=self.params.prediction_type,
                    tensorboard=False,
                    n_classes=self.params.class_number)

        return model

    # ****************************************************************************************
    def train(self, pipeline):
        """Trains a neural net model for multiple epochs, choose the epoch with the best validation
        set performance, refits the model for that number of epochs, and saves the tuned model.

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Side effects:
            Sets the following attributes for DCNNModelWrapper:
                data (ModelDataset): contains the dataset, set in pipeline
                best_epoch (int): Initialized as None, keeps track of the epoch with the best validation score
                train_perf_data (list of PerfData): Initialized as an empty array, 
                    contains the predictions and performance of the training dataset
                valid_perf_data (list of PerfData): Initialized as an empty array,
                    contains the predictions and performance of the validation dataset
                train_epoch_perfs (np.array): Initialized as an empty array,
                    contains a list of dictionaries of predicted values and metrics on the training dataset
                valid_epoch_perfs (np.array of dicts): Initialized as an empty array,
                    contains a list of dictionaries of predicted values and metrics on the validation dataset
        """
        # TODO: Fix docstrings above
        num_folds = len(pipeline.data.train_valid_dsets)
        if num_folds > 1:
            self.train_kfold_cv(pipeline)
        else:
            self.train_with_early_stopping(pipeline)

    # ****************************************************************************************
    def train_with_early_stopping(self, pipeline):
        """Trains a neural net model for up to self.params.max_epochs epochs, while tracking the validation
        set metric given by params.model_choice_score_type. Saves a model checkpoint each time the metric
        is improved over its previous saved value by more than a threshold percentage. If the metric fails to
        improve for more than a specified 'patience' number of epochs, stop training and revert the model state
        to the last saved checkpoint. 

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Side effects:
            Sets the following attributes for DCNNModelWrapper:
                data (ModelDataset): contains the dataset, set in pipeline
                best_epoch (int): Initialized as None, keeps track of the epoch with the best validation score
                best_validation_score (float): The best validation model choice score attained during training.
                train_perf_data (list of PerfData): Initialized as an empty array, 
                    contains the predictions and performance of the training dataset
                valid_perf_data (list of PerfData): Initialized as an empty array,
                    contains the predictions and performance of the validation dataset
                train_epoch_perfs (np.array): A standard training set performance metric (r2_score or roc_auc), at the end of each epoch.
                valid_epoch_perfs (np.array): A standard validation set performance metric (r2_score or roc_auc), at the end of each epoch.
        """
        self.data = pipeline.data
        self.best_epoch = 0
        self.best_valid_score = None
        self.early_stopping_min_improvement = self.params.early_stopping_min_improvement
        self.early_stopping_patience = self.params.early_stopping_patience
        self.train_epoch_perfs = np.zeros(self.params.max_epochs)
        self.valid_epoch_perfs = np.zeros(self.params.max_epochs)
        self.test_epoch_perfs = np.zeros(self.params.max_epochs)
        self.train_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.valid_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.test_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.model_choice_scores = np.zeros(self.params.max_epochs)

        self.train_perf_data = []
        self.valid_perf_data = []
        self.test_perf_data = []

        for ei in range(self.params.max_epochs):
            self.train_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'train'))
            self.valid_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'valid'))
            self.test_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'test'))

        test_dset = pipeline.data.test_dset

        time_limit = int(self.params.slurm_time_limit)
        training_start = time.time()

        train_dset, valid_dset = pipeline.data.train_valid_dsets[0]
        for ei in range(self.params.max_epochs):
            if llnl_utils.is_lc_system() and (ei > 0):
                # If we're running on an LC system, check that we have enough time to complete another epoch
                # before the current job finishes, by extrapolating from the time elapsed so far.

                now = time.time() 
                elapsed_time = now - pipeline.start_time
                training_time = now - training_start
                time_remaining = time_limit * 60 - elapsed_time
                time_needed = training_time/ei

                if time_needed > 0.9 * time_remaining:
                    self.log.warn("Projected time to finish one more epoch exceeds time left in job; cutting training to %d epochs" %
                                    ei)
                    self.params.max_epochs = ei
                    break

            # Train the model for one epoch. We turn off automatic checkpointing, so the last checkpoint
            # saved will be the one we created intentionally when we reached a new best validation score.
            self.model.fit(train_dset, nb_epoch=1, checkpoint_interval=0)
            train_pred = self.model.predict(train_dset, [])
            valid_pred = self.model.predict(valid_dset, [])
            test_pred = self.model.predict(test_dset, [])

            train_perf = self.train_perf_data[ei].accumulate_preds(train_pred, train_dset.ids)
            valid_perf = self.valid_perf_data[ei].accumulate_preds(valid_pred, valid_dset.ids)
            test_perf = self.test_perf_data[ei].accumulate_preds(test_pred, test_dset.ids)
            self.log.info("Epoch %d: training %s = %.3f, validation %s = %.3f, test %s = %.3f" % (
                          ei, pipeline.metric_type, train_perf, pipeline.metric_type, valid_perf,
                          pipeline.metric_type, test_perf))

            # Compute performance metrics for each subset, and check if we've reached a new best validation set score

            self.train_epoch_perfs[ei], _ = self.train_perf_data[ei].compute_perf_metrics()
            self.valid_epoch_perfs[ei], _ = self.valid_perf_data[ei].compute_perf_metrics()
            self.test_epoch_perfs[ei], _ = self.test_perf_data[ei].compute_perf_metrics()
            valid_score = self.valid_perf_data[ei].model_choice_score(self.params.model_choice_score_type)
            self.model_choice_scores[ei] = valid_score
            self.num_epochs_trained = ei + 1
            if self.best_valid_score is None:
                self.model.save_checkpoint()
                self.best_valid_score = valid_score
                self.best_epoch = ei
            elif valid_score - self.best_valid_score > self.early_stopping_min_improvement:
                # Save a new checkpoint
                self.model.save_checkpoint()
                self.best_valid_score = valid_score
                self.best_epoch = ei
            elif ei - self.best_epoch > self.early_stopping_patience:
                self.log.info(f"No improvement after {self.early_stopping_patience} epochs, stopping training")
                break

        # Revert to last checkpoint
        self.model.restore()
        self.model.save()

        # Only copy the model files we need, not the entire directory
        self._copy_model(self.best_model_dir)
        self.log.info(f"Best model from epoch {self.best_epoch} saved to {self.best_model_dir}")



    # ****************************************************************************************
    def train_kfold_cv(self, pipeline):
        """Trains a neural net model with K-fold cross-validation for a specified number of epochs.
        Finds the epoch with the best validation set performance averaged over folds, then refits 
        a model for the same number of epochs to the combined training and validation data.

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Side effects:
            Sets the following attributes for DCNNModelWrapper:
                data (ModelDataset): contains the dataset, set in pipeline
                best_epoch (int): Initialized as None, keeps track of the epoch with the best validation score
                train_perf_data (list of PerfData): Initialized as an empty array, 
                    contains the predictions and performance of the training dataset
                valid_perf_data (list of PerfData): Initialized as an empty array,
                    contains the predictions and performance of the validation dataset
                train_epoch_perfs (np.array): Contains a standard training set performance metric (r2_score or roc_auc), averaged over folds,
                    at the end of each epoch.
                valid_epoch_perfs (np.array): Contains a standard validation set performance metric (r2_score or roc_auc), averaged over folds,
                    at the end of each epoch.
        """
        # TODO: Fix docstrings above
        num_folds = len(pipeline.data.train_valid_dsets)
        self.data = pipeline.data
        self.best_epoch = 0
        self.best_valid_score = None
        self.train_epoch_perfs = np.zeros(self.params.max_epochs)
        self.valid_epoch_perfs = np.zeros(self.params.max_epochs)
        self.test_epoch_perfs = np.zeros(self.params.max_epochs)
        self.train_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.valid_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.test_epoch_perf_stds = np.zeros(self.params.max_epochs)
        self.model_choice_scores = np.zeros(self.params.max_epochs)
        self.early_stopping_min_improvement = self.params.early_stopping_min_improvement
        self.early_stopping_patience = self.params.early_stopping_patience


        # Create PerfData structures for computing cross-validation metrics
        self.valid_perf_data = []
        for ei in range(self.params.max_epochs):
            self.valid_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'valid'))

        test_dset = pipeline.data.test_dset

        time_limit = int(self.params.slurm_time_limit)
        training_start = time.time()

        # Train a separate model for each fold
        models = []
        for k in range(num_folds):
            models.append(self.recreate_model())

        for ei in range(self.params.max_epochs):
            
            if llnl_utils.is_lc_system() and (ei > 0):
                # If we're running on an LC system, check that we have enough time to complete another epoch
                # across all folds, plus rerun the training, before the current job finishes, by 
                # extrapolating from the time elapsed so far.
    
                now = time.time() 
                elapsed_time = now - pipeline.start_time
                training_time = now - training_start
                time_remaining = time_limit * 60 - elapsed_time

                # epochs_remaining is how many epochs we have to run if we do one more across all folds,
                # then do self.best_epoch+1 epochs on the combined training & validation set, allowing for the
                # possibility that the next epoch may be the best one.

                epochs_remaining = ei + 2
                time_per_epoch = training_time/ei
                time_needed = epochs_remaining * time_per_epoch
    
                if time_needed > 0.9 * time_remaining:
                    self.log.warn('Projected time to finish one more epoch exceeds time left in job; cutting training to %d epochs' % ei)
                    self.params.max_epochs = ei
                    break


            # Create PerfData structures that are only used within loop to compute metrics during initial training
            train_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'train')
            test_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'test')
            for k in range(num_folds):
                self.model = models[k]
                train_dset, valid_dset = pipeline.data.train_valid_dsets[k]

                # We turn off automatic checkpointing - we only want to save a checkpoints for the final model.
                self.model.fit(train_dset, nb_epoch=1, checkpoint_interval=0, restore=False)
                train_pred = self.model.predict(train_dset, [])
                valid_pred = self.model.predict(valid_dset, [])
                test_pred = self.model.predict(test_dset, [])

                train_perf = train_perf_data.accumulate_preds(train_pred, train_dset.ids)
                valid_perf = self.valid_perf_data[ei].accumulate_preds(valid_pred, valid_dset.ids)
                test_perf = test_perf_data.accumulate_preds(test_pred, test_dset.ids)
                self.log.info("Fold %d, epoch %d: training %s = %.3f, validation %s = %.3f, test %s = %.3f" % (
                              k, ei, pipeline.metric_type, train_perf, pipeline.metric_type, valid_perf,
                              pipeline.metric_type, test_perf))

            # Compute performance metrics for current epoch across validation sets for all folds, and update
            # the best_epoch and best score if the new score exceeds the previous best score by a specified
            # threshold.

            self.valid_epoch_perfs[ei], self.valid_epoch_perf_stds[ei] = self.valid_perf_data[ei].compute_perf_metrics()
            valid_score = self.valid_perf_data[ei].model_choice_score(self.params.model_choice_score_type)
            self.model_choice_scores[ei] = valid_score
            self.num_epochs_trained = ei + 1
            if self.best_valid_score is None:
                self.best_valid_score = valid_score
                self.best_epoch = ei
                self.log.info(f"Total cross-validation score for epoch {ei} is {valid_score:.3}")
            elif valid_score - self.best_valid_score > self.early_stopping_min_improvement:
                self.best_valid_score = valid_score
                self.best_epoch = ei
                self.log.info(f"*** Total cross-validation score for epoch {ei} is {valid_score:.3}, is new maximum")
            elif ei - self.best_epoch > self.early_stopping_patience:
                self.log.info(f"No improvement after {self.early_stopping_patience} epochs, stopping training")
                break
            else:
                self.log.info(f"Total cross-validation score for epoch {ei} is {valid_score:.3}")

        # Train a new model for best_epoch epochs on the combined training/validation set. Compute the training and test
        # set metrics at each epoch.
        fit_dataset = pipeline.data.combined_training_data()
        retrain_start = time.time()
        self.model = self.recreate_model()
        self.log.info(f"Best epoch was {self.best_epoch}, retraining with combined training/validation set")

        self.train_perf_data = []
        self.test_perf_data = []
        for ei in range(self.best_epoch+1):
            self.train_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'train_valid'))
            self.test_perf_data.append(perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'test'))

            self.model.fit(fit_dataset, nb_epoch=1, checkpoint_interval=0, restore=False)
            train_pred = self.model.predict(fit_dataset, [])
            test_pred = self.model.predict(test_dset, [])
            train_perf = self.train_perf_data[ei].accumulate_preds(train_pred, fit_dataset.ids)
            test_perf = self.test_perf_data[ei].accumulate_preds(test_pred, test_dset.ids)
            self.log.info(f"Combined folds: Epoch {ei}, training {pipeline.metric_type} = {train_perf:.3},"
                         + f"test {pipeline.metric_type} = {test_perf:.3}")
            self.train_epoch_perfs[ei], self.train_epoch_perf_stds[ei] = self.train_perf_data[ei].compute_perf_metrics()
            self.test_epoch_perfs[ei], self.test_epoch_perf_stds[ei] = self.test_perf_data[ei].compute_perf_metrics()
        self.model.save_checkpoint()
        self.model.save()

        # Only copy the model files we need, not the entire directory
        self._copy_model(self.best_model_dir)
        retrain_time = time.time() - retrain_start
        self.log.info("Time to retrain model for %d epochs: %.1f seconds, %.1f sec/epoch" % (self.best_epoch, retrain_time, 
                       retrain_time/self.best_epoch))

    # ****************************************************************************************
    def _copy_model(self, dest_dir):
        """Copies the files needed to recreate a DeepChem NN model from the current model
        directory to a destination directory.

        Args:
            dest_dir (str): The destination directory for the model files
        """

        chkpt_file = os.path.join(self.model_dir, 'checkpoint')
        with open(chkpt_file, 'r') as chkpt_in:
            chkpt_dict = yaml.load(chkpt_in.read())
        chkpt_prefix = chkpt_dict['model_checkpoint_path']
        files = [chkpt_file]
        files.append(os.path.join(self.model_dir, 'model.pickle'))
        files.append(os.path.join(self.model_dir, '%s.index' % chkpt_prefix))
        files.append(os.path.join(self.model_dir, '%s.meta' % chkpt_prefix))
        files = files + glob.glob(os.path.join(self.model_dir, '%s.data-*' % chkpt_prefix))
        self._clean_up_excess_files(dest_dir)
        for file in files:
            shutil.copy2(file, dest_dir)
        self.log.info("Saved model files to '%s'" % dest_dir)


    # ****************************************************************************************
    def reload_model(self, reload_dir):
        """Loads a saved neural net model from the specified directory.

        Args:
            reload_dir (str): Directory where saved model is located.
            model_dataset (ModelDataset Object): contains the current full dataset

        Side effects:
            Resets the value of model, transformers, and transformers_x
        """
        if self.params.featurizer == 'graphconv':
            self.model = dc.models.GraphConvModel.load_from_dir(reload_dir, restore=False)
        elif self.params.prediction_type == 'regression':
            self.model = MultitaskRegressor.load_from_dir(reload_dir, restore=False)
        else:
            self.model = MultitaskClassifier.load_from_dir(reload_dir, restore=False)
        # Hack to run models trained in DeepChem 2.1 with DeepChem 2.2
        self.model.default_outputs = self.model.outputs
        # Get latest checkpoint path transposed to current model dir
        ckpt = tf.train.get_checkpoint_state(reload_dir)
        if os.path.exists(f"{ckpt.model_checkpoint_path}.index"):
            checkpoint = ckpt.model_checkpoint_path
        else:
            checkpoint = os.path.join(reload_dir, os.path.basename(ckpt.model_checkpoint_path))
        self.model.restore(checkpoint=checkpoint)


        # Load transformers if they would have been saved with the model
        if trans.transformers_needed(self.params) and (self.params.transformer_key is not None):
            self.log.info("Reloading transformers from file %s" % self.params.transformer_key)
            if self.params.save_results:
                self.transformers, self.transformers_x = dsf.retrieve_dataset_by_datasetkey(
                    dataset_key = self.params.transformer_key,
                    bucket = self.params.transformer_bucket,
                    client = self.ds_client )
            else:
                self.transformers, self.transformers_x = pickle.load(open( self.params.transformer_key, 'rb' ))


    # ****************************************************************************************
    def get_pred_results(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset. subset may be 'train', 'valid', 'test'
        accordingly.  epoch_label indicates the training epoch we want results for; currently the
        only option for this is 'best'.  Results are returned as a dictionary of parameter, value pairs.

        Args:
            subset (str): Label for the current subset of the dataset (choices ['train','valid','test','full'])
            epoch_label (str): Label for the training epoch we want results for (choices ['best'])

        Returns:
            dict: A dictionary of parameter/ value pairs of the prediction values and results of the dataset subset

        Raises:
            ValueError: if epoch_label not in ['best']
            ValueError: If subset not in ['train','valid','test','full']
        """
        if subset == 'full':
            return self.get_full_dataset_pred_results(self.data)
        if epoch_label == 'best':
            epoch = self.best_epoch
            model_dir = self.best_model_dir
        else:
            raise ValueError("Unknown epoch_label '%s'" % epoch_label)
        if subset == 'train':
            return self.get_train_valid_pred_results(self.train_perf_data[epoch])
        elif subset == 'valid':
            return self.get_train_valid_pred_results(self.valid_perf_data[epoch])
        elif subset == 'test':
            return self.get_train_valid_pred_results(self.test_perf_data[epoch])
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)

    # ****************************************************************************************
    def get_perf_data(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset. subset may be 'train', 'valid', 'test' or 'full',
        epoch_label indicates the training epoch we want results for; currently the
        only option for this is 'best'. Results are returned as a PerfData object of the appropriate class 
        for the model's split strategy and prediction type.

        Args:
            subset (str): Label for the current subset of the dataset (choices ['train','valid','test','full'])
            epoch_label (str): Label for the training epoch we want results for (choices ['best'])

        Returns:
            PerfData object: Performance object pulled from the appropriate subset

        Raises:
            ValueError: if epoch_label not in ['best']
            ValueError: If subset not in ['train','valid','test','full']
        """

        if subset == 'full':
            return self.get_full_dataset_perf_data(self.data)
        if epoch_label == 'best':
            epoch = self.best_epoch
            model_dir = self.best_model_dir
        else:
            raise ValueError("Unknown epoch_label '%s'" % epoch_label)

        if subset == 'train':
            return self.train_perf_data[epoch]
        elif subset == 'valid':
            return self.valid_perf_data[epoch]
        elif subset == 'test':
            #return self.get_test_perf_data(model_dir, self.data)
            return self.test_perf_data[epoch]
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)



    # ****************************************************************************************
    def generate_predictions(self, dataset):
        """Generates predictions for specified dataset with current model, as well as standard deviations
        if params.uncertainty=True

        Args:
            dataset: the deepchem DiskDataset to generate predictions for

        Returns:
            (pred, std): tuple of predictions for compounds and standard deviation estimates, if requested.
            Each element of tuple is a numpy array of shape (ncmpds, ntasks, nclasses), where nclasses = 1 for regression
            models.
        """
        pred, std = None, None
        self.log.info("Predicting values for current model")

        # For deepchem's predict_uncertainty function, you are not allowed to specify transformers. That means that the
        # predictions are being made in the transformed space, not the original space. We call undo_transforms() to generate
        # the transformed predictions. To transform the standard deviations, we rely on the fact that at present we only use
        # dc.trans.NormalizationTransformer (which centers and scales the data).

        # Uncertainty is now supported by DeepChem's GraphConv, at least for regression models.
        # if self.params.uncertainty and self.params.prediction_type == 'regression' and self.params.featurizer != 'graphconv':

        # Current (2.1) DeepChem neural net classification models don't support uncertainties.
        if self.params.uncertainty and self.params.prediction_type == 'classification':
            self.log.warning("Warning: DeepChem neural net models support uncertainty for regression only.")
 
        if self.params.uncertainty and self.params.prediction_type == 'regression':
            # For multitask, predict_uncertainty returns a list of (pred, std) tuples, one for each task.
            # For singletask, it returns one tuple. Convert the result into a pair of ndarrays of shape (ncmpds, ntasks, nclasses).
            pred_std = self.model.predict_uncertainty(dataset)
            if type(pred_std) == tuple:
                #JEA
                #ntasks = 1
                ntasks = len(pred_std[0][0])
                pred, std = pred_std
                pred = pred.reshape((pred.shape[0], 1, pred.shape[1]))
                std = std.reshape(pred.shape)
            else:
                ntasks = len(pred_std)
                pred0, std0 = pred_std[0]
                ncmpds = pred0.shape[0]
                nclasses = pred0.shape[1]
                pred = np.concatenate([p.reshape((ncmpds, 1, nclasses)) for p, s in pred_std], axis=1)
                std = np.concatenate([s.reshape((ncmpds, 1, nclasses)) for p, s in pred_std], axis=1)

            if self.params.transformers and self.transformers is not None:
                  # Transform the standard deviations, if we can. This is a bit of a hack, but it works for
                # NormalizationTransformer, since the standard deviations used to scale the data are
                # stored in the transformer object.
                if len(self.transformers) == 1 and (isinstance(self.transformers[0], dc.trans.NormalizationTransformer) or isinstance(self.transformers[0],trans.NormalizationTransformerMissingData)):
                    y_stds = self.transformers[0].y_stds.reshape((1,ntasks,1))
                    std = std / y_stds
                pred = dc.trans.undo_transforms(pred, self.transformers)
        elif self.params.transformers and self.transformers is not None:
            pred = self.model.predict(dataset, self.transformers)
            if self.params.prediction_type == 'regression':
                pred = pred.reshape((pred.shape[0], pred.shape[1], 1))
        else:
            pred = self.model.predict(dataset, [])
            if self.params.prediction_type == 'regression':
                pred = pred.reshape((pred.shape[0], pred.shape[1], 1))
        return pred, std

    # ****************************************************************************************
    def get_model_specific_metadata(self):
        """Returns a dictionary of parameter settings for this ModelWrapper object that are specific
        to neural network models.

        Returns:
            model_spec_metdata (dict): A dictionary of the parameter sets for the DCNNModelWrapper object.
                Parameters are saved under the key 'nn_specific' as a subdictionary.
        """
        nn_metadata = dict(
                    best_epoch = self.best_epoch,
                    max_epochs = self.params.max_epochs,
                    batch_size = self.params.batch_size,
                    optimizer_type = self.params.optimizer_type,
                    layer_sizes = self.params.layer_sizes,
                    dropouts = self.params.dropouts,
                    weight_init_stddevs = self.params.weight_init_stddevs,
                    bias_init_consts = self.params.bias_init_consts,
                    learning_rate = self.params.learning_rate,
                    weight_decay_penalty=self.params.weight_decay_penalty,
                    weight_decay_penalty_type=self.params.weight_decay_penalty_type
        )
        model_spec_metadata = dict(nn_specific = nn_metadata)
        return model_spec_metadata

    # ****************************************************************************************
    def _clean_up_excess_files(self, dest_dir):
        """
        Function to clean up extra model files left behind in the training process.
        Only removes self.model_dir
        """
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir)
        os.mkdir(dest_dir)
        
# ****************************************************************************************
class DCRFModelWrapper(ModelWrapper):
    """Contains methods to load in a dataset, split and featurize the data, fit a model to the train dataset,
    generate predictions for an input dataset, and generate performance metrics for these predictions.


    Attributes:
        Set in __init__
            params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
            featurization (Featurization object): The featurization object created outside of model_wrapper
            log (log): The logger
            output_dir (str): The parent path of the model directory
            transformers (list): Initialized as an empty list, stores the transformers on the response col
            transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers
            model_dir (str): The subdirectory under output_dir that contains the model. Created in setup_model_dirs.
            best_model_dir (str): The subdirectory under output_dir that contains the best model. Created in setup_model_dirs
            model: The dc.models.sklearn_models.SklearnModel as specified by the params attribute

        Created in train:
            data (ModelDataset): contains the dataset, set in pipeline
            best_epoch (int): Set to 0, not applicable to deepchem random forest models
            train_perf_data (PerfData): Contains the predictions and performance of the training dataset
            valid_perf_data (PerfData): Contains the predictions and performance of the validation dataset
            train_perfs (dict): A dictionary of predicted values and metrics on the training dataset
            valid_perfs (dict): A dictionary of predicted values and metrics on the training dataset

    """

    def __init__(self, params, featurizer, ds_client):
        """Initializes DCRFModelWrapper object.

        Args:
            params (Namespace object): contains all parameter information.
            featurizer (Featurization): Object managing the featurization of compounds
            ds_client: datastore client.
        """
        super().__init__(params, featurizer, ds_client)
        self.best_model_dir = os.path.join(self.output_dir, 'best_model')
        self.model_dir = self.best_model_dir
        os.makedirs(self.best_model_dir, exist_ok=True)

        if self.params.prediction_type == 'regression':
            rf_model = RandomForestRegressor(n_estimators=self.params.rf_estimators,
                                             max_features=self.params.rf_max_features,
                                             max_depth=self.params.rf_max_depth,
                                             n_jobs=-1)
        else:
            rf_model = RandomForestClassifier(n_estimators=self.params.rf_estimators,
                                              max_features=self.params.rf_max_features,
                                              max_depth=self.params.rf_max_depth,
                                              n_jobs=-1)

        self.model = dc.models.sklearn_models.SklearnModel(rf_model, model_dir=self.best_model_dir)

    # ****************************************************************************************
    def train(self, pipeline):
        """Trains a random forest model and saves the trained model.

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Returns:
            None

        Side effects:
            data (ModelDataset): contains the dataset, set in pipeline
            best_epoch (int): Set to 0, not applicable to deepchem random forest models
            train_perf_data (PerfData): Contains the predictions and performance of the training dataset
            valid_perf_data (PerfData): Contains the predictions and performance of the validation dataset
            train_perfs (dict): A dictionary of predicted values and metrics on the training dataset
            valid_perfs (dict): A dictionary of predicted values and metrics on the training dataset
        """

        self.data = pipeline.data
        self.best_epoch = None
        self.train_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers,'train')
        self.valid_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'valid')
        self.test_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'test')

        self.log.info("Fitting random forest model")

        test_dset = pipeline.data.test_dset

        num_folds = len(pipeline.data.train_valid_dsets)
        for k in range(num_folds):
            train_dset, valid_dset = pipeline.data.train_valid_dsets[k]
            self.model.fit(train_dset)

            train_pred = self.model.predict(train_dset, [])
            train_perf = self.train_perf_data.accumulate_preds(train_pred, train_dset.ids)

            valid_pred = self.model.predict(valid_dset, [])
            valid_perf = self.valid_perf_data.accumulate_preds(valid_pred, valid_dset.ids)

            test_pred = self.model.predict(test_dset, [])
            test_perf = self.test_perf_data.accumulate_preds(test_pred, test_dset.ids)
            self.log.info("Fold %d: training %s = %.3f, validation %s = %.3f, test %s = %.3f" % (
                          k, pipeline.metric_type, train_perf, pipeline.metric_type, valid_perf,
                             pipeline.metric_type, test_perf))


        # Compute mean and SD of performance metrics across validation sets for all folds
        self.train_perf, self.train_perf_std = self.train_perf_data.compute_perf_metrics()
        self.valid_perf, self.valid_perf_std = self.valid_perf_data.compute_perf_metrics()
        self.test_perf, self.test_perf_std = self.test_perf_data.compute_perf_metrics()

        # Compute score to be used for ranking model hyperparameter sets
        self.model_choice_score = self.valid_perf_data.model_choice_score(self.params.model_choice_score_type)

        if num_folds > 1:
            # For k-fold CV, retrain on the combined training and validation sets
            fit_dataset = self.data.combined_training_data()
            self.model.fit(fit_dataset, restore=False)
        self.model.save()
        # The best model is just the single RF training run.
        self.best_epoch = 0

    # ****************************************************************************************
    def reload_model(self, reload_dir):
        """Loads a saved random forest model from the specified directory. Also loads any transformers that
        were saved with it.

        Args:
            reload_dir (str): Directory where saved model is located.
            model_dataset (ModelDataset Object): contains the current full dataset

        Side effects:
            Resets the value of model, transformers, and transformers_x

        """
        if self.params.prediction_type == 'regression':
            rf_model = RandomForestRegressor(n_estimators=self.params.rf_estimators,
                                             max_features=self.params.rf_max_features,
                                             max_depth=self.params.rf_max_depth,
                                             n_jobs=-1)
        else:
            rf_model = RandomForestClassifier(n_estimators=self.params.rf_estimators,
                                              max_features=self.params.rf_max_features,
                                              max_depth=self.params.rf_max_depth,
                                              n_jobs=-1)

        # Load transformers if they would have been saved with the model
        if trans.transformers_needed(self.params) and (self.params.transformer_key is not None):
            self.log.info("Reloading transformers from file %s" % self.params.transformer_key)
            if self.params.save_results:
                self.transformers, self.transformers_x = dsf.retrieve_dataset_by_datasetkey(dataset_key = self.params.transformer_key,
                               bucket = self.params.transformer_bucket,
                               client= self.ds_client )
            else:
                self.transformers, self.transformers_x = pickle.load(open( self.params.transformer_key, 'rb' ))
        self.model = dc.models.sklearn_models.SklearnModel(rf_model, model_dir=reload_dir)
        self.model.reload()

    # ****************************************************************************************
    def get_pred_results(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset.

        Args:
            subset: 'train', 'valid', 'test' or 'full' accordingly.
            epoch_label: ignored; this function always returns the results for the current model.

        Returns:
            A dictionary of parameter, value pairs, in the format expected by the
            prediction_results element of the ModelMetrics data.

        Raises:
            ValueError: if subset not in ['train','valid','test','full']
            
        """
        if subset == 'train':
            return self.get_train_valid_pred_results(self.train_perf_data)
        elif subset == 'valid':
            return self.get_train_valid_pred_results(self.valid_perf_data)
        elif subset == 'test':
            return self.get_train_valid_pred_results(self.test_perf_data)
        elif subset == 'full':
            return self.get_full_dataset_pred_results(self.data)
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)


    # ****************************************************************************************
    def get_perf_data(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset.

        Args:
            subset (str): may be 'train', 'valid', 'test' or 'full'
            epoch_label (not used in random forest, but kept as part of the method structure)

        Results:
            PerfData object: Subclass of perfdata object associated with the appropriate subset's split strategy and prediction type.

        Raises:
            ValueError: if subset not in ['train','valid','test','full']
        """
        if subset == 'train':
            return self.train_perf_data
        elif subset == 'valid':
            return self.valid_perf_data
        elif subset == 'test':
            #return self.get_test_perf_data(self.best_model_dir, self.data)
            return self.test_perf_data
        elif subset == 'full':
            return self.get_full_dataset_perf_data(self.data)
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)


    # ****************************************************************************************
    def generate_predictions(self, dataset):
        """Generates predictions for specified dataset, as well as uncertainty values if params.uncertainty=True

        Args:
            dataset: the deepchem DiskDataset to generate predictions for

        Returns:
            (pred, std): numpy arrays containing predictions for compounds and the standard error estimates.

        """
        pred, std = None, None
        self.log.info("Evaluating current model")

        pred = self.model.predict(dataset, self.transformers)
        ncmpds = pred.shape[0]
        pred = pred.reshape((ncmpds,1,-1))

        if self.params.uncertainty:
            if self.params.prediction_type == 'regression':
                rf_model = joblib.load(os.path.join(self.best_model_dir, 'model.joblib'))
                ## s.d. from forest
                if self.params.transformers and self.transformers is not None:
                    RF_per_tree_pred = [dc.trans.undo_transforms(
                        tree.predict(dataset.X), self.transformers) for tree in rf_model.estimators_]
                else:
                    RF_per_tree_pred = [tree.predict(dataset.X) for tree in rf_model.estimators_]

                # Don't need to "untransform" standard deviations here, since they're calculated from
                # the untransformed per-tree predictions.
                std = np.array([np.std(col) for col in zip(*RF_per_tree_pred)]).reshape((ncmpds,1,-1))
            else:
                # We can estimate uncertainty for binary classifiers, but not multiclass (yet)
                nclasses = pred.shape[2]
                if nclasses == 2:
                    ntrees = self.params.rf_estimators
                    # Use normal approximation to binomial sampling error. Later we can do Jeffrey's interval if we
                    # want to get fancy.
                    std = np.sqrt(pred * (1-pred) / ntrees)
                else:
                    self.log.warning("Warning: Random forest only supports uncertainties for binary classifiers.")

        return pred, std

    # ****************************************************************************************
    def get_model_specific_metadata(self):
        """Returns a dictionary of parameter settings for this ModelWrapper object that are specific
        to random forest models.

        Returns:
            model_spec_metadata (dict): Returns random forest specific metadata as a subdict under the key 'rf_specific'

        """
        rf_metadata = {
            'rf_estimators': self.params.rf_estimators,
            'rf_max_features': self.params.rf_max_features,
            'rf_max_depth': self.params.rf_max_depth
        }
        model_spec_metadata = dict(rf_specific = rf_metadata)
        return model_spec_metadata
    
    # ****************************************************************************************
    def _clean_up_excess_files(self, dest_dir):
        """
        Function to clean up extra model files left behind in the training process.
        Does not apply to Random Forest.
        """
        return

# ****************************************************************************************
class DCxgboostModelWrapper(ModelWrapper):
    """Contains methods to load in a dataset, split and featurize the data, fit a model to the train dataset,
    generate predictions for an input dataset, and generate performance metrics for these predictions.


    Attributes:
        Set in __init__
            params (argparse.Namespace): The argparse.Namespace parameter object that contains all parameter information
            featurization (Featurization object): The featurization object created outside of model_wrapper
            log (log): The logger
            output_dir (str): The parent path of the model directory
            transformers (list): Initialized as an empty list, stores the transformers on the response col
            transformers_x (list): Initialized as an empty list, stores the transformers on the featurizers
            model_dir (str): The subdirectory under output_dir that contains the model. Created in setup_model_dirs.
            best_model_dir (str): The subdirectory under output_dir that contains the best model. Created in setup_model_dirs
            model: The dc.models.sklearn_models.SklearnModel as specified by the params attribute

        Created in train:
            data (ModelDataset): contains the dataset, set in pipeline
            best_epoch (int): Set to 0, not applicable
            train_perf_data (PerfObjects): Contains the predictions and performance of the training dataset
            valid_perf_data (PerfObjects): Contains the predictions and performance of the validation dataset
            train_perfs (dict): A dictionary of predicted values and metrics on the training dataset
            valid_perfs (dict): A dictionary of predicted values and metrics on the validation dataset

    """

    def __init__(self, params, featurizer, ds_client):
        """Initializes RunModel object.

        Args:
            params (Namespace object): contains all parameter information.
            featurizer (Featurization): Object managing the featurization of compounds
            ds_client: datastore client.
        """
        super().__init__(params, featurizer, ds_client)
        self.best_model_dir = os.path.join(self.output_dir, 'best_model')
        self.model_dir = self.best_model_dir
        os.makedirs(self.best_model_dir, exist_ok=True)

        if self.params.prediction_type == 'regression':
            xgb_model = xgb.XGBRegressor(max_depth=self.params.xgb_max_depth,
                                         learning_rate=self.params.xgb_learning_rate,
                                         n_estimators=self.params.xgb_n_estimators,
                                         silent=True,
                                         objective='reg:squarederror',
                                         booster='gbtree',
                                         gamma=self.params.xgb_gamma,
                                         min_child_weight=self.params.xgb_min_child_weight,
                                         max_delta_step=0,
                                         subsample=self.params.xgb_subsample,
                                         colsample_bytree=self.params.xgb_colsample_bytree,
                                         colsample_bylevel=1,
                                         reg_alpha=0,
                                         reg_lambda=1,
                                         scale_pos_weight=1,
                                         base_score=0.5,
                                         random_state=0,
                                         missing=None,
                                         importance_type='gain',
                                         n_jobs=-1,
                                         gpu_id = 0,
                                         n_gpus = -1,
                                         max_bin = 16,
#                                          tree_method = 'gpu_hist'
                                         )
        else:
            xgb_model = xgb.XGBClassifier(max_depth=self.params.xgb_max_depth,
                                         learning_rate=self.params.xgb_learning_rate,
                                         n_estimators=self.params.xgb_n_estimators,
                                          silent=True,
                                          objective='binary:logistic',
                                          booster='gbtree',
                                          gamma=self.params.xgb_gamma,
                                          min_child_weight=self.params.xgb_min_child_weight,
                                          max_delta_step=0,
                                          subsample=self.params.xgb_subsample,
                                          colsample_bytree=self.params.xgb_colsample_bytree,
                                          colsample_bylevel=1,
                                          reg_alpha=0,
                                          reg_lambda=1,
                                          scale_pos_weight=1,
                                          base_score=0.5,
                                          random_state=0,
                                          importance_type='gain',
                                          missing=None,
                                          gpu_id = 0,
                                          n_jobs=-1,                                          
                                          n_gpus = -1,
                                          max_bin = 16,
#                                           tree_method = 'gpu_hist'
                                         )

        self.model = dc.models.xgboost_models.XGBoostModel(xgb_model, model_dir=self.best_model_dir)

    # ****************************************************************************************
    def train(self, pipeline):
        """Trains a xgboost model and saves the trained model.

        Args:
            pipeline (ModelPipeline): The ModelPipeline instance for this model run.

        Returns:
            None

        Side effects:
            data (ModelDataset): contains the dataset, set in pipeline
            best_epoch (int): Set to 0, not applicable to deepchem xgboost models
            train_perf_data (PerfData): Contains the predictions and performance of the training dataset
            valid_perf_data (PerfData): Contains the predictions and performance of the validation dataset
            train_perfs (dict): A dictionary of predicted values and metrics on the training dataset
            valid_perfs (dict): A dictionary of predicted values and metrics on the training dataset
        """

        self.data = pipeline.data
        self.best_epoch = None
        self.train_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers,'train')
        self.valid_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'valid')
        self.test_perf_data = perf.create_perf_data(self.params.prediction_type, pipeline.data, self.transformers, 'test')

        self.log.info("Fitting xgboost model")

        test_dset = pipeline.data.test_dset

        num_folds = len(pipeline.data.train_valid_dsets)
        for k in range(num_folds):
            train_dset, valid_dset = pipeline.data.train_valid_dsets[k]
            self.model.fit(train_dset)

            train_pred = self.model.predict(train_dset, [])
            train_perf = self.train_perf_data.accumulate_preds(train_pred, train_dset.ids)

            valid_pred = self.model.predict(valid_dset, [])
            valid_perf = self.valid_perf_data.accumulate_preds(valid_pred, valid_dset.ids)

            test_pred = self.model.predict(test_dset, [])
            test_perf = self.test_perf_data.accumulate_preds(test_pred, test_dset.ids)
            self.log.info("Fold %d: training %s = %.3f, validation %s = %.3f, test %s = %.3f" % (
                          k, pipeline.metric_type, train_perf, pipeline.metric_type, valid_perf,
                             pipeline.metric_type, test_perf))

        # Compute mean and SD of performance metrics across validation sets for all folds
        self.train_perf, self.train_perf_std = self.train_perf_data.compute_perf_metrics()
        self.valid_perf, self.valid_perf_std = self.valid_perf_data.compute_perf_metrics()
        self.test_perf, self.test_perf_std = self.test_perf_data.compute_perf_metrics()

        # Compute score to be used for ranking model hyperparameter sets
        self.model_choice_score = self.valid_perf_data.model_choice_score(self.params.model_choice_score_type)

        if num_folds > 1:
            # For k-fold CV, retrain on the combined training and validation sets
            fit_dataset = self.data.combined_training_data()
            self.model.fit(fit_dataset, restore=False)
        self.model.save()
        # The best model is just the single xgb training run.
        self.best_epoch = 0

    # ****************************************************************************************
    def reload_model(self, reload_dir):

        """Loads a saved xgboost model from the specified directory. Also loads any transformers that
        were saved with it.

        Args:
            reload_dir (str): Directory where saved model is located.
            model_dataset (ModelDataset Object): contains the current full dataset

        Side effects:
            Resets the value of model, transformers, and transformers_x

        """

        if self.params.prediction_type == 'regression':
            xgb_model = xgb.XGBRegressor(max_depth=self.params.xgb_max_depth,
                                         learning_rate=self.params.xgb_learning_rate,
                                         n_estimators=self.params.xgb_n_estimators,
                                         silent=True,
                                         objective='reg:squarederror',
                                         booster='gbtree',
                                         gamma=self.params.xgb_gamma,
                                         min_child_weight=self.params.xgb_min_child_weight,
                                         max_delta_step=0,
                                         subsample=self.params.xgb_subsample,
                                         colsample_bytree=self.params.xgb_colsample_bytree,
                                         colsample_bylevel=1,
                                         reg_alpha=0,
                                         reg_lambda=1,
                                         scale_pos_weight=1,
                                         base_score=0.5,
                                         random_state=0,
                                         missing=None,
                                         importance_type='gain',
                                         n_jobs=-1,
                                         gpu_id = 0,
                                         n_gpus = -1,
                                         max_bin = 16,
#                                          tree_method = 'gpu_hist'
                                         )
        else:
            xgb_model = xgb.XGBClassifier(max_depth=self.params.xgb_max_depth,
                                         learning_rate=self.params.xgb_learning_rate,
                                         n_estimators=self.params.xgb_n_estimators,
                                          silent=True,
                                          objective='binary:logistic',
                                          booster='gbtree',
                                          gamma=self.params.xgb_gamma,
                                          min_child_weight=self.params.xgb_min_child_weight,
                                          max_delta_step=0,
                                          subsample=self.params.xgb_subsample,
                                          colsample_bytree=self.params.xgb_colsample_bytree,
                                          colsample_bylevel=1,
                                          reg_alpha=0,
                                          reg_lambda=1,
                                          scale_pos_weight=1,
                                          base_score=0.5,
                                          random_state=0,
                                          importance_type='gain',
                                          missing=None,
                                          gpu_id = 0,
                                          n_jobs=-1,                                          
                                          n_gpus = -1,
                                          max_bin = 16,
#                                           tree_method = 'gpu_hist',
                                         )

        # Load transformers if they would have been saved with the model
        if trans.transformers_needed(self.params) and (self.params.transformer_key is not None):
            self.log.warning("Reloading transformers from file %s" % self.params.transformer_key)
            if self.params.save_results:
                self.transformers, self.transformers_x = dsf.retrieve_dataset_by_datasetkey(
                    dataset_key=self.params.transformer_key,
                    bucket=self.params.transformer_bucket,
                    client=self.ds_client)
            else:
                self.transformers, self.transformers_x = pickle.load(open(self.params.transformer_key, 'rb'))

        self.model = dc.models.xgboost_models.XGBoostModel(xgb_model, model_dir=self.best_model_dir)
        self.model.reload()

    # ****************************************************************************************
    def get_pred_results(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset.

        Args:
            subset: 'train', 'valid', 'test' or 'full' accordingly.
            epoch_label: ignored; this function always returns the results for the current model.

        Returns:
            A dictionary of parameter, value pairs, in the format expected by the
            prediction_results element of the ModelMetrics data.

        Raises:
            ValueError: if subset not in ['train','valid','test','full']

        """
        if subset == 'train':
            return self.get_train_valid_pred_results(self.train_perf_data)
        elif subset == 'valid':
            return self.get_train_valid_pred_results(self.valid_perf_data)
        elif subset == 'test':
            return self.get_train_valid_pred_results(self.test_perf_data)
        elif subset == 'full':
            return self.get_full_dataset_pred_results(self.data)
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)

    # ****************************************************************************************
    def get_perf_data(self, subset, epoch_label=None):
        """Returns predicted values and metrics from a training, validation or test subset
        of the current dataset, or the full dataset.

        Args:
            subset (str): may be 'train', 'valid', 'test' or 'full'
            epoch_label (not used in random forest, but kept as part of the method structure)

        Results:
            PerfData object: Subclass of perfdata object associated with the appropriate subset's split strategy and prediction type.

        Raises:
            ValueError: if subset not in ['train','valid','test','full']
        """

        if subset == 'train':
            return self.train_perf_data
        elif subset == 'valid':
            return self.valid_perf_data
        elif subset == 'test':
            #return self.get_test_perf_data(self.best_model_dir, self.data)
            return self.test_perf_data
        elif subset == 'full':
            return self.get_full_dataset_perf_data(self.data)
        else:
            raise ValueError("Unknown dataset subset '%s'" % subset)

    # ****************************************************************************************
    def generate_predictions(self, dataset):
        """Generates predictions for specified dataset, as well as uncertainty values if params.uncertainty=True

        Args:
            dataset: the deepchem DiskDataset to generate predictions for

        Returns:
            (pred, std): numpy arrays containing predictions for compounds and the standard error estimates.

        """
        pred, std = None, None
        self.log.warning("Evaluating current model")

        pred = self.model.predict(dataset, self.transformers)
        ncmpds = pred.shape[0]
        pred = pred.reshape((ncmpds, 1, -1))
        self.log.warning("uncertainty not supported by xgboost models")

        return pred, std

    # ****************************************************************************************
    def get_model_specific_metadata(self):
        """Returns a dictionary of parameter settings for this ModelWrapper object that are specific
        to xgboost models.

        Returns:
            model_spec_metadata (dict): Returns xgboost specific metadata as a subdict under the key 'xgb_specific'

        """
        xgb_metadata = {"xgb_max_depth" : self.params.xgb_max_depth,
                       "xgb_learning_rate" : self.params.xgb_learning_rate,
                       "xgb_n_estimators" : self.params.xgb_n_estimators,
                       "xgb_gamma" : self.params.xgb_gamma,
                       "xgb_min_child_weight" : self.params.xgb_min_child_weight,
                       "xgb_subsample" : self.params.xgb_subsample,
                       "xgb_colsample_bytree"  :self.params.xgb_colsample_bytree
                        }
        model_spec_metadata = dict(xgb_specific=xgb_metadata)
        return model_spec_metadata

    # ****************************************************************************************
    def _clean_up_excess_files(self, dest_dir):
        """
        Function to clean up extra model files left behind in the training process.
        Does not apply to xgboost
        """
        return
