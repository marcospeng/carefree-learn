import os
import cflearn
import platform
import unittest

import numpy as np

from cfdata.tabular import TabularDataset

IS_LINUX = platform.system() == "Linux"


num_jobs = 0 if IS_LINUX else 2
logging_folder = "__test_dist__"
kwargs = {"min_epoch": 1, "num_epoch": 2, "max_epoch": 4}


class TestDist(unittest.TestCase):
    def test_experiments(self) -> None:
        x, y = TabularDataset.iris().xy
        exp_folder = os.path.join(logging_folder, "__test_experiments__")
        experiments = cflearn.Experiments(exp_folder)
        experiments.add_task(x, y, model="fcnn", **kwargs)  # type: ignore
        experiments.add_task(x, y, model="fcnn", **kwargs)  # type: ignore
        experiments.add_task(x, y, model="tree_dnn", **kwargs)  # type: ignore
        experiments.add_task(x, y, model="tree_dnn", **kwargs)  # type: ignore
        experiments.run_tasks(num_jobs=num_jobs)
        ms = cflearn.transform_experiments(experiments)
        saving_folder = os.path.join(logging_folder, "__test_experiments_save__")
        experiments.save(saving_folder)
        loaded = cflearn.Experiments.load(saving_folder)
        ms_loaded = cflearn.transform_experiments(loaded)
        self.assertTrue(
            np.allclose(ms["fcnn"][1].predict(x), ms_loaded["fcnn"][1].predict(x))
        )
        cflearn._rmtree(logging_folder)

    def test_benchmark(self) -> None:
        benchmark_folder = os.path.join(logging_folder, "__test_benchmark__")
        x, y = TabularDataset.iris().xy
        benchmark = cflearn.Benchmark(
            "foo",
            "clf",
            models=["fcnn", "tree_dnn"],
            temp_folder=benchmark_folder,
            increment_config=kwargs.copy(),
        )
        benchmarks = {
            "fcnn": {"default": {}, "sgd": {"optimizer": "sgd"}},
            "tree_dnn": {"default": {}, "adamw": {"optimizer": "adamw"}},
        }
        results = benchmark.k_fold(
            3,
            x,
            y,
            num_jobs=num_jobs,
            benchmarks=benchmarks,  # type: ignore
        )
        msg1 = results.comparer.log_statistics()
        saving_folder = os.path.join(logging_folder, "__test_benchmark_save__")
        benchmark.save(saving_folder)
        loaded_benchmark, loaded_results = cflearn.Benchmark.load(saving_folder)
        msg2 = loaded_results.comparer.log_statistics()
        self.assertEqual(msg1, msg2)
        cflearn._rmtree(logging_folder)


if __name__ == "__main__":
    unittest.main()
