# coding=utf-8
'''
Copyright 2018 Supun Nakandala and Arun Kumar
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''
import math, time

from pyspark import SparkConf, SparkContext, StorageLevel
from pyspark.sql import SQLContext
from pyspark.sql.functions import col, lit, array

from cnn.alexnet import AlexNet
from cnn.resnet50 import ResNet50
from cnn.vgg16 import VGG16

from vista_utils import get_dir_size, get_struct_df, get_images_df, get_joined_features, image_to_byte_arr_udf, \
    get_image_features_for_layer, get_feature_projections, serialize_cnn_features_udf, \
    get_all_image_features, slice_layers_udf

import sys
sys.path.append('../code/python')
sys.path.append('../code/python/cnn')

from pyspark.ml import Pipeline
from pyspark.ml.classification import LogisticRegression, LinearSVC, DecisionTreeClassifier, GBTClassifier, RandomForestClassifier, MultilayerPerceptronClassifier, OneVsRest
from pyspark.ml.evaluation import MulticlassClassificationEvaluator
from pyspark.ml.feature import StringIndexer
from pyspark.ml.tuning import CrossValidator, ParamGridBuilder

def downstream_ml_func(features_df, results_dict, layer_index, model_name='LogisticRegression', extra_config={}):

    def hyperparameter_tuned_model(clf, train_df):
	pipeline = Pipeline(stages=[clf])

        paramGrid = ParamGridBuilder()
        for i in extra_config:
	    if i == 'numFolds':
                continue
            paramGrid = paramGrid.addGrid(eval('clf.'+i), extra_config[i])

        paramGrid = paramGrid.build()

	if 'numFolds' in extra_config:
	    numFolds = extra_config['numFolds']
	else:
	    numFolds = 3 # default

        crossval = CrossValidator(estimator=pipeline,
                      estimatorParamMaps=paramGrid,
                      evaluator=MulticlassClassificationEvaluator(),
                      numFolds=numFolds)
        # Run cross-validation, and choose the best set of parameters.
        return crossval.fit(train_df)

    train_df, test_df = features_df.randomSplit([0.8, 0.2], seed=2019)

    if model_name == 'LogisticRegression':
        clf = LogisticRegression(labelCol="label", featuresCol="features", maxIter=10, regParam=0.1)

    if model_name == 'LinearSVC':
        clf = LinearSVC(maxIter=5, regParam=0.01)
    
    if model_name == 'DecisionTreeClassifier':
        stringIndexer = StringIndexer(inputCol="label", outputCol="indexed")
        si_model = stringIndexer.fit(train_df)
        train_df = si_model.transform(train_df)
        
	clf = DecisionTreeClassifier(maxDepth=2, labelCol="indexed")

    if model_name == 'GBTClassifier':
        stringIndexer = StringIndexer(inputCol="label", outputCol="indexed")
        si_model = stringIndexer.fit(train_df)
        train_df = si_model.transform(train_df)
        
	clf = GBTClassifier(labelCol="label", featuresCol="features", maxIter=50, maxDepth=5)
    
    if model_name == 'RandomForestClassifier':
        stringIndexer = StringIndexer(inputCol="label", outputCol="indexed")
        si_model = stringIndexer.fit(train_df)
        td = si_model.transform(train_df)
        
	clf = RandomForestClassifier(labelCol="label", featuresCol="features")
    
    if model_name == 'OneVsRest':
        lr = LogisticRegression(labelCol="label", featuresCol="features", maxIter=50, regParam=0.5)
        clf = OneVsRest(labelCol="label", featuresCol="features", predictionCol="prediction", classifier=lr)
    
    if extra_config != {}:
        model = hyperparameter_tuned_model(clf, train_df)
    else:
        model = clf.fit(train_df)

    predictions = model.transform(test_df)

    evaluator = MulticlassClassificationEvaluator(labelCol="label", predictionCol="prediction",
                metricName="accuracy")
    results_dict[layer_index] = evaluator.evaluate(predictions)
    return results_dict

class Vista(object):
    """
        Vista Optimizer Class
    """

    """Vista Optimizer Constants. For details refer the technical report (
    https://adalabucsd.github.io/papers/TR_2018_Vista.pdf) """
    alpha_1 = 1.2
    alpha_2 = 2.0

    max_broadcast = 0.1
    max_partition_size = 0.1

    mem_sys_rsv = 3
    mem_spark_user_rsv = 0.2
    mem_spark_core_min = 2.4
    mem_spark_user_ml_model = 0.5

    model_footprints = {
        'alexnet': {'ser': 0.3, 'runtime': 2},
        'vgg16': {'ser': 0.6, 'runtime': 3},
        'resnet50': {'ser': 0.2, 'runtime': 1}
    }

    def __init__(self, name, mem_sys, cpu_sys, n_nodes, model, n_layers, start_layer, struct_input,
                 image_input, n_records, dS, mem_sys_rsv=3, enable_sys_config_optzs=True, gpu=False, tot_gpu_mem=0, model_name='LogisticRegression', extra_config={}):
        """
            Initializing the Vista Optimizer
        :param name: Name for the Spark job
        :param mem_sys: Amount of memory available in s system node
        :param cpu_sys: Number of CPUs available in a system node
        :param n_nodes: Number of nodes in the Spark cluster
        :param model:   CNN model name
        :param n_layers: Number of layers in the CNN to be explored
        :param start_layer: Layer index of the CNN input. Zero means input is raw images
        :param struct_input: HDFS path to the structured input file
        :param image_input: HDFS path to the image data dir on HDFS
        :param n_records: Number of records in the dataset
        :param dS:  Number of structured features
        :param mem_sys_rsv: Amount of memory to be reserved as system reserved memory
        :param enable_sys_config_optzs: Whether to enable system configurations optimizations (spark configurations and physical plan operators)
        :param gpu: GPU available
        :param tot_gpu_mem: If GPU availabel total GPU memory
	:param ml_model: Name of the (PySpark MLLib) Downstream ML Model to run in the Vista optimizer
	:param extra_config: Extra configuration settings for hyperparameter tuning with the downstream model
        """
        self.name = name
        self.mem_sys = math.floor(mem_sys)
        self.cpu_sys = cpu_sys
        self.n_nodes = n_nodes
        self.model = model
        self.n_layers = n_layers
        self.start_layer = start_layer
        self.struct_input = struct_input
        self.image_input = image_input
        self.n_records = n_records
        self.dS = dS
        self.mem_sys_rsv = mem_sys_rsv
        self.enable_sys_config_optzs = enable_sys_config_optzs
        self.gpu = gpu
        self.tot_gpu_mem = tot_gpu_mem
	self.model_name = model_name
	self.extra_config = extra_config

        self.inf = 'staged'
        self.operator = 'after-join'
        self.join = self.__get_join()


        if(self.enable_sys_config_optzs):
            self.cpu_spark = self.__get_cpu_spark()
            self.num_partitions = self.__get_num_partitions(self.cpu_spark)
            self.heap = int(self.__get_heap_size())
            self.core_memory_fraction = self.__get_spark_core_memory_fraction()
            self.persistence = self.__get_persistence_format()
            if self.persistence == 'ser':
                self.storage_level = StorageLevel(True, True, False, False)
            else:
                self.storage_level = StorageLevel(True, True, False, True)
        else:
            self.cpu_spark = cpu_sys
            self.num_partitions = -1
            self.heap = mem_sys - mem_sys_rsv
            self.core_memory_fraction = 0.6
            self.persistence = self.__get_persistence_format()
            self.storage_level = StorageLevel(True, True, False, True)


    def __config_spark(self):
        conf = SparkConf()
        conf.setAppName(self.name)
        conf.set("spark.executor.memory", str(self.heap) + "g")
        conf.set("spark.memory.fraction", self.core_memory_fraction)
        conf.set("spark.executor.cores", self.cpu_spark)
        conf.set("spark.cores.max", self.n_nodes*self.cpu_spark)

        conf.set("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
        conf.set("spark.shuffle.reduceLocality.enabled", "false")

        if self.enable_sys_config_optzs and self.num_partitions > 0:
            image_dir_size = get_dir_size(self.image_input)
            if self.num_partitions > image_dir_size / 10485760:
                conf.set("spark.files.maxPartitionBytes", str(int(math.ceil(image_dir_size / self.num_partitions))))
            else:
                conf.set("spark.files.maxPartitionBytes", "10485760")  # 10MB
        else:
            conf.set("spark.files.maxPartitionBytes", "10485760")  # 10MB

        sc = SparkContext.getOrCreate(conf=conf)
        sql_context = SQLContext(sc)

        if self.enable_sys_config_optzs:
            sql_context.sql("SET spark.sql.autoBroadcastJoinThreshold = -1")
        if self.enable_sys_config_optzs and self.num_partitions > 0:
            sql_context.sql("SET spark.sql.shuffle.partitions = " + str(self.num_partitions))


	if self.model_name == 'OneVsRest' and self.extra_config != {}:
	    raise Exception('OneVsRest does not support any additional configurations and extra_config needs to be ignored with OneVsRest.')

	if self.extra_config != {}:
            mdl = eval(self.model_name)()
            for i in self.extra_config:
		if i == 'numFolds':
		    continue
                if not mdl.hasParam(i):
                    raise AttributeError(self.model_name + ' has no attribute \'' + i + '\'')
                if type(self.extra_config[i]) != list:
                    raise TypeError('The specified parameter(s) of ', i, 'in extra_config must be in a list!')

        return sc, sql_context

    def run(self):
        """
            Launch the CNN feature transfer workload
        :return:
        """
        sc, sql_context = self.__config_spark()

	print(
        'Vista Configs(join, cpu, np, heap, f_core, pers): ' + ", ".join([str(x) for x in [self.join, self.cpu_spark,
                                                                                           self.num_partitions,
                                                                                           self.heap,
                                                                                           self.core_memory_fraction,
                                                                                           self.persistence]]))

        # using a pre-materialized layer
        if (self.start_layer != 0):
            return self.__run_with_pre_mat(sc, sql_context)

        struct_df = get_struct_df(sc, self.struct_input)
        images_df = get_images_df(sc, self.image_input)
        evaluation_results = {}

        if self.inf == 'bulk':
            if self.operator == 'before-join':
                images_df = images_df.select(col('id'),
                                             image_to_byte_arr_udf(sc, col('image_buffer')).alias('input_layer'))
                image_features_df, cum_sizes, shapes = get_all_image_features(self.model, images_df,
                                                                              self.n_layers)
                features_df = get_joined_features(image_features_df, struct_df, self.join == 'b')
            elif self.operator == 'after-join':
                joined_df = get_joined_features(
                    images_df.select("id", col("image_buffer").alias("image_features")), struct_df, self.join == 'b') \
                    .select("id", "features", image_to_byte_arr_udf(sc, col('image_features')).alias('input_layer'),
                            "label")
                features_df, cum_sizes, shapes = get_all_image_features(self.model, joined_df, self.n_layers)

            features_df = features_df.select("id", "features", "image_features", "label")
            features_df._jdf.persist(sc._getJavaStorageLevel(self.storage_level))

            sliced_features_df = features_df.withColumn("cumulative_sizes", array([lit(x) for x in cum_sizes]))
            sliced_features_df = sliced_features_df.withColumn(
                "image_features", slice_layers_udf(sc, col('image_features'), col('cumulative_sizes')))

            # evaluate the models
            shapes.reverse()
            for merged_features_df, layer_index in zip(
                    get_feature_projections(sc, sliced_features_df, self.n_layers, shapes),
                    range(1, 1 + self.n_layers)):
                evaluation_results = downstream_ml_func(merged_features_df, evaluation_results, -1 * layer_index, model_name=self.model_name, extra_config=self.extra_config)

            features_df._jdf.unpersist()
        elif self.inf == 'staged':
            starting_layer = 0
            joined = False
            features_df_prev = input_df = None
            for i in reversed(range(1, self.n_layers + 1)):
                layer_index = -1 * i
                if not joined:
                    joined = True
                    if self.operator == 'before-join':
                        images_df = images_df.select(col('id'), image_to_byte_arr_udf(sc, col('image_buffer')).alias(
                            'input_layer'))
                        image_features_df, shape = get_image_features_for_layer(self.model, layer_index, images_df,
                                                                                starting_layer, False)
                        features_df = get_joined_features(image_features_df, struct_df, self.join == 'b')
                    elif self.operator == 'after-join':
                        joined_df = get_joined_features(
                            images_df.select("id", col("image_buffer").alias("image_features")), struct_df,
                            self.join == 'b') \
                            .select("id", "features",
                                    image_to_byte_arr_udf(sc, col('image_features')).alias('input_layer'), "label")
                        features_df, shape = get_image_features_for_layer(self.model, layer_index, joined_df,
                                                                          starting_layer)
                else:
                    features_df, shape = get_image_features_for_layer(self.model, layer_index, input_df, starting_layer)

                features_df = features_df.select("id", "features", "image_features", "label")
                features_df._jdf.persist(sc._getJavaStorageLevel(self.storage_level))

                merged_features_df = get_feature_projections(sc, features_df, 1, [shape])[0]

                evaluation_results = downstream_ml_func(merged_features_df, evaluation_results, layer_index, model_name=self.model_name, extra_config=self.extra_config)

                if features_df_prev is not None: features_df_prev._jdf.unpersist()
                features_df_prev = features_df

                input_df = features_df.select(col('id'), serialize_cnn_features_udf(sc, col('image_features'))
                                              .alias('input_layer'), col('features'), col('label'))
                starting_layer = layer_index

        return evaluation_results

    # using a pre-materialized layer
    def __run_with_pre_mat(self, sc, sql_context):
        struct_df = get_struct_df(sc, self.struct_input)
        images_df = sql_context.read.parquet(self.image_input)

        features_df = images_df.alias('x') \
            .join(struct_df.alias('y'), col('x.id') == col('y.id')) \
            .select('x.id', col('x.input_layer').alias('image_features'), 'y.features', 'y.label')

        evaluation_results = {}
        if self.start_layer == -1 * self.n_layers:
            features_df._jdf.persist(sc._getJavaStorageLevel(self.storage_level))
            if self.model == 'alexnet':
                shape = AlexNet.transfer_layers_shapes[self.start_layer]
            elif self.model == 'vgg16':
                shape = VGG16.transfer_layers_shapes[self.start_layer]
            elif self.model == 'resnet50':
                shape = ResNet50.transfer_layers_shapes[self.start_layer]

            merged_features_df = get_feature_projections(sc, features_df, 1, [shape])[0]
            evaluation_results = downstream_ml_func(merged_features_df, evaluation_results, self.start_layer, model_name=self.model_name, extra_config=self.extra_config)

        input_df = features_df.select(col('id'), col('features'), col('label'),
                                      serialize_cnn_features_udf(sc, col('image_features')).alias('input_layer'))

        num_layers_to_explore = self.n_layers - 1
        prev_features_df = features_df
        if self.inf == 'bulk':
            features_df, cum_sizes, shapes = get_all_image_features(self.model, input_df, num_layers_to_explore,
                                                                    self.start_layer)
            features_df = features_df.select("id", "features", "image_features", "label")
            features_df._jdf.persist(sc._getJavaStorageLevel(self.storage_level))

            sliced_features_df = features_df.withColumn("cumulative_sizes", array([lit(x) for x in cum_sizes]))
            sliced_features_df = sliced_features_df.withColumn(
                "image_features", slice_layers_udf(sc, col('image_features'), col('cumulative_sizes')))

            # evaluate the models
            shapes.reverse()
            for merged_features_df, layer_index in zip(
                    get_feature_projections(sc, sliced_features_df, num_layers_to_explore, shapes),
                    range(1, 1 + self.n_layers)):
                evaluation_results = downstream_ml_func(merged_features_df, evaluation_results, -1 * layer_index, model_name=self.model_name, extra_config=self.extra_config)
                prev_features_df._jdf.unpersist()

            features_df._jdf.unpersist()
        elif self.inf == 'staged':
            for i in reversed(range(1, num_layers_to_explore + 1)):
                layer_index = -1 * i
                features_df, shape = get_image_features_for_layer(self.model, layer_index, input_df, layer_index - 1,
                                                                  True)
                features_df = features_df.select("id", "features", "image_features", "label")
                features_df._jdf.persist(sc._getJavaStorageLevel(self.storage_level))

                merged_features_df = get_feature_projections(sc, features_df, 1, [shape])[0]
                evaluation_results = downstream_ml_func(merged_features_df, evaluation_results, layer_index, model_name=self.model_name, extra_config=self.extra_config)

                prev_features_df._jdf.unpersist()
                prev_features_df = features_df
                input_df = features_df.select(col('id'), serialize_cnn_features_udf(sc, col('image_features'))
                                              .alias('input_layer'), col('features'), col('label'))

        return evaluation_results

    def override_inference_type(self, inf):
        self.inf = inf

    def overrdide_operator_placement(self, operator):
        self.operator = operator

    def __get_join(self):
        size = Vista.alpha_1 * self.dS * 4 * 1 * self.n_records / 1024 / 1024 / 1024
        if size < Vista.max_broadcast:
            return 'b'
        else:
            return 's'

    def override_join(self, join):
        self.join = join

    def __get_cpu_spark(self):
        if self.gpu:
            #TODO Here the same CPU runtime footprint is taken as the GPU footprint. This is a conservative estimate and if
            #TODO a better estimate can be obtained by profiling
            cpu_max = int(min(math.floor(self.tot_gpu_mem/Vista.model_footprints[self.model]['runtime']), self.cpu_sys))
        else:
            cpu_max = self.cpu_sys

        for i in reversed(range(1, cpu_max)):
            heap = self.mem_sys - Vista.mem_sys_rsv - i * Vista.model_footprints[self.model]['runtime']
            user = i * max((Vista.model_footprints[self.model]['ser'] + Vista.alpha_2 * Vista.max_partition_size),
                           Vista.mem_spark_user_ml_model) + Vista.mem_spark_user_rsv
            core = heap - 0.3 - user
            if core >= Vista.mem_spark_core_min:
                return i

    def override_cpu_spark(self, cpu):
        self.cpu_spark = cpu

    def __get_num_partitions(self, cpu):
        size = self.__get_largest_intermediate_table_size()
        total_cores = cpu * self.n_nodes
        return int(math.ceil(size / Vista.max_partition_size / total_cores) * total_cores)

    def override_num_partitions(self, np):
        self.num_partitions = np

    def __get_heap_size(self):
        return self.mem_sys - Vista.mem_sys_rsv - self.cpu_spark * Vista.model_footprints[self.model]['runtime']

    def override_heap_size(self, heap):
        self.heap = heap

    def __get_spark_core_memory_fraction(self):
        user = self.cpu_spark * max(
            (Vista.model_footprints[self.model]['ser'] + Vista.alpha_2 * Vista.max_partition_size),
            Vista.mem_spark_user_ml_model) + Vista.mem_spark_user_rsv
        core = self.heap - 0.3 - user
        return (1.0 * core) / (core + user)

    def override_spark_core_memory_fraction(self, core_mem_fraction):
        self.core_memory_fraction = core_mem_fraction

    def __get_persistence_format(self):
        size = self.__get_two_largest_stored_intermediate_table_sizes()
        total_storage = self.heap * self.core_memory_fraction * 0.5 * self.n_nodes
        if size > total_storage:
            return 'ser'
        else:
            return 'deser'

    def override_persistence_format(self, pers):
        self.persistence = pers
        if self.persistence == 'ser':
            self.storage_level = StorageLevel(True, True, False, False)
        else:
            self.storage_level = StorageLevel(True, True, False, True)

    def __get_largest_intermediate_table_size(self):
        if self.model == 'resnet50':
            n_features = ResNet50.transfer_layer_flattened_sizes[self.n_layers - 1]
        elif self.model == 'alexnet':
            n_features = AlexNet.transfer_layer_flattened_sizes[self.n_layers - 1]
        elif self.model == 'vgg16':
            n_features = VGG16.transfer_layer_flattened_sizes[self.n_layers - 1]
        return Vista.alpha_2 * (max(n_features, 227 * 227 * 3) + self.dS) * 4 * self.n_records / 1024 / 1024 / 1024

    def __get_two_largest_stored_intermediate_table_sizes(self):
        if self.model == 'resnet50':
            n_features = sum(ResNet50.transfer_layer_flattened_sizes[self.n_layers - 1: self.n_layers - 2])
        elif self.model == 'alexnet':
            n_features = sum(AlexNet.transfer_layer_flattened_sizes[self.n_layers - 1: self.n_layers - 2])
        elif self.model == 'vgg16':
            n_features = sum(VGG16.transfer_layer_flattened_sizes[self.n_layers - 1: self.n_layers - 2])
        return Vista.alpha_2 * max(n_features + self.dS, 227 * 227 * 3) * 4 * self.n_records / 1024 / 1024 / 1024


if __name__ == "__main__":
    prev_time = time.time()
    # mem_sys_rsv is an optional parameter. If not set a default value of 3 will be used.
    vista = Vista("vista-example", 32, 8, 8, 'alexnet', 3, 0, 'hdfs://spark-master:9000/foods_sample.csv',
                      'hdfs://spark-master:9000/foods_images', 20129, 130, mem_sys_rsv=3, model_name='LogisticRegression', extra_config={})

    # Optional overrides
    #vista.override_inference_type('bulk')
    #vista.override_join('s')
    #vista.overrdide_operator_placement('before-join')

    print(vista.run())
    print("Runtime: " + str((time.time()-prev_time)/60.0))
