#!/usr/bin/env python    
# -*- coding: utf-8 -*- 

#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
################################################################################
#
#
################################################################################

# =============================================================================
# HeteroSecureBoostingHost 
# =============================================================================

from federatedml.feature import Quantile
from federatedml.tree import HeteroDecisionTreeHost
from federatedml.tree import BoostingTree
from federatedml.tree import BoostingTreeModelMeta
from federatedml.util import HeteroSecureBoostingTreeTransferVariable
from federatedml.util import consts
from numpy import random
from arch.api import federation
from arch.api import eggroll
from arch.api.utils import log_utils

LOGGER = log_utils.getLogger()


class HeteroSecureBoostingTreeHost(BoostingTree):
    def __init__(self, secureboost_tree_param):
        super(HeteroSecureBoostingTreeHost, self).__init__(secureboost_tree_param)

        self.transfer_inst = HeteroSecureBoostingTreeTransferVariable()
        self.flowid = 0
        self.tree_dim = None
        self.feature_num = None
        self.trees_ = []
        self.bin_split_points = None
        self.bin_sparse_points = None
        self.data_bin = None

    def convert_feature_to_bin(self, data_instance):
        LOGGER.info("convert feature to bins")
        self.data_bin, self.bin_split_points, self.bin_sparse_points = \
            Quantile.convert_feature_to_bin(
                data_instance, self.quantile_method, self.bin_num,
                self.bin_gap, self.bin_sample_num)

    def sample_valid_features(self):
        LOGGER.info("sample valid features")
        if self.feature_num is None:
            self.feature_num = self.bin_split_points.shape[0]

        choose_feature = random.choice(range(0, self.feature_num), \
                                       max(1, int(self.subsample_feature_rate * self.feature_num)), replace=False)

        valid_features = [False for i in range(self.feature_num)]
        for fid in choose_feature:
            valid_features[fid] = True
        return valid_features

    def set_flowid(self, flowid=0):
        LOGGER.info("set flowid, flowid is {}".format(flowid))
        self.flowid = flowid

    def generate_flowid(self, round_num, tree_num):
        LOGGER.info("generate encrypter")
        return ".".join(map(str, [self.flowid, round_num, tree_num]))

    def sync_tree_dim(self):
        LOGGER.info("sync tree dim from guest")
        self.tree_dim = federation.get(name=self.transfer_inst.tree_dim.name,
                                       tag=self.transfer_inst.generate_transferid(self.transfer_inst.tree_dim),
                                       idx=0)
        LOGGER.info("tree dim is %d" % (self.tree_dim))

    def sync_stop_flag(self, num_round):
        LOGGER.info("sync stop flag from guest, boosting round is {}".format(num_round))
        stop_flag = federation.get(name=self.transfer_inst.stop_flag.name,
                                   tag=self.transfer_inst.generate_transferid(self.transfer_inst.stop_flag, num_round),
                                   idx=0)

        return stop_flag

    def fit(self, data_inst):
        LOGGER.info("begin to train secureboosting guest model")
        self.convert_feature_to_bin(data_inst)
        self.sync_tree_dim()

        for i in range(self.num_trees):
            n_tree = []
            for tidx in range(self.tree_dim):
                tree_inst = HeteroDecisionTreeHost(self.tree_param)

                tree_inst.set_inputinfo(data_bin=self.data_bin, bin_split_points=self.bin_split_points,
                                        bin_sparse_points=self.bin_sparse_points)

                valid_features = self.sample_valid_features()
                tree_inst.set_flowid(self.generate_flowid(i, tidx))
                tree_inst.set_valid_features(valid_features)

                tree_inst.fit()
                n_tree.append(tree_inst.get_tree_model())

            self.trees_.append(n_tree)

            if self.n_iter_no_change is True:
                stop_flag = self.sync_stop_flag(i)
                if stop_flag:
                    break

        LOGGER.info("end to train secureboosting guest model")

    def predict(self, data_inst, predict_param=None):
        LOGGER.info("start predict")
        for i in range(len(self.trees_)):
            n_tree = self.trees_[i]
            for tidx in range(len(n_tree)):
                tree_inst = HeteroDecisionTreeHost(self.tree_param)
                tree_inst.set_tree_model(n_tree[tidx])
                tree_inst.set_flowid(self.generate_flowid(i, tidx))

                tree_inst.predict(data_inst)

        LOGGER.info("end predict")

    def save_model(self, model_table, model_namespace):
        LOGGER.info("save model")
        modelmeta = BoostingTreeModelMeta()
        modelmeta.trees_ = self.trees_
        modelmeta.loss_type = self.loss_type
        modelmeta.tree_dim = self.tree_dim
        modelmeta.task_type = self.task_type

        model = eggroll.parallelize([modelmeta], include_key=False)
        model.save_as(model_table, model_namespace)

    def load_model(self, model_table, model_namespace):
        LOGGER.info("load model")
        modelmeta = list(eggroll.table(model_table, model_namespace).collect())[0][1]
        self.task_type = modelmeta.task_type
        self.loss_type = modelmeta.loss_type
        self.tree_dim = modelmeta.tree_dim
        self.trees_ = modelmeta.trees_
