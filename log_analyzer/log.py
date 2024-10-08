"""
Copyright (c) 2021, Alibaba Group;
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
   http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import pickle
import dataclasses
import numpy as np
from typing import Union, Dict, List
from utils.utils import CommType, CommGroup
from log_analyzer.utils import convert_size_to_msg, calc_bw_log


@dataclasses.dataclass
class LogItem:
    comm_type: CommType = dataclasses.field(default=None)
    comm_group: CommGroup = dataclasses.field(default=None)
    comm_group_size: int = dataclasses.field(default=None)
    msg_size: float = dataclasses.field(default=0)

    stage: str = dataclasses.field(default="")
    dst: int = dataclasses.field(default=None)
    src: int = dataclasses.field(default=None)
    additional: str = dataclasses.field(default="")

    _elapsed_time: float = dataclasses.field(default=None)
    algbw: float = dataclasses.field(default=None)
    busbw: float = dataclasses.field(default=None)
    count: float = dataclasses.field(default=1)

    @property
    def elapsed_time(self) -> float:
        return self._elapsed_time

    @elapsed_time.setter
    def elapsed_time(self, elapsed_time):
        self._elapsed_time = elapsed_time
        self.algbw, self.busbw = calc_bw_log(
            self.comm_type, self.msg_size, elapsed_time
        )

    def is_epoch_end(self):
        return self.comm_type == CommType.epoch_end

    def is_workload(self):
        return self.elapsed_time is None

    def view_as_ds_log(self):
        log_str = f"[RANK 0] comm op: {self.comm_type} | comm group: {self.comm_group}"
        log_str += " | time (ms): {:.2f}".format(self.elapsed_time)
        log_str += " | msg size: " + convert_size_to_msg(self.msg_size)
        log_str += " | algbw (Gbps): {:.2f} ".format(self.algbw)
        log_str += " | busbw (Gbps): {:.2f} ".format(self.busbw)
        return log_str

    def csv_header(self):
        return ",".join([k for k in self.__dict__.keys()])

    def view_as_csv_line(self):
        return ",".join([str(getattr(self, k)) for k in self.__dict__.keys()])

    def __str__(self):
        if self.is_workload():
            return "None"
        return "None"


def _print_stage_log(
    stage_name: str,
    stage_count: int,
    comm_type_info: Dict,
    primary_key: List[str],
    agg_key: List[str],
    performance_key: List[str],
):
    log_str = ""
    for pkey in sorted(comm_type_info.keys()):
        log_str += f"stage: {stage_name}"
        for i, pkey_name in enumerate(primary_key):
            value = pkey[i] if pkey_name != "msg_size" else convert_size_to_msg(pkey[i])
            log_str += f" | {pkey_name}: {value}"
        for key in agg_key:
            value = comm_type_info[pkey][key]
            value = convert_size_to_msg(value) if key == "msg_size" else f"{value:.2f}"
            log_str += f" | {key}: {value}"
        for key in performance_key:
            performance_value_list = sorted(comm_type_info[pkey][key])
            log_str += f" | {key}: {np.mean(performance_value_list):.2f}±{np.std(performance_value_list):.2f}"
            log_str += f" | min{key}: {performance_value_list[0]:.2f}"
            log_str += f" | max{key}: {performance_value_list[-1]:.2f}"
            log_str += f" | p90{key}: {performance_value_list[-len(performance_value_list)//9]:.2f}"
        log_str += "\n"
    return log_str


def _analyze_stage_log(comm_log: List[Dict], stage: str, comm_info: Dict[str, Dict]):
    def __update_info(
        info_dict,
        log,
        primary_key: List[str],
        agg_key: List[str],
        performance_key: List[str],
    ):
        primary_key = tuple(log[key] for key in primary_key)
        if primary_key not in info_dict:
            info_dict[primary_key] = dict((key, 0) for key in agg_key)
            info_dict[primary_key].update(dict((key, []) for key in performance_key))
        for key in agg_key:
            info_dict[primary_key][key] += log[key]
        for key in performance_key:
            info_dict[primary_key][key].append(log[key])

    if stage not in comm_info:
        comm_info[stage] = {
            "count": 0,
            "comm_type_info": {},
            "detailed_comm_type_info": {},
        }
    comm_info[stage]["count"] += 1
    # key: comm_type, value: count, time_ms
    comm_type_info = comm_info[stage]["comm_type_info"]
    # key: comm_type, msg_size, value: count, time_ms
    detailed_comm_type_info = comm_info[stage]["detailed_comm_type_info"]
    for log in comm_log:
        __update_info(
            comm_type_info,
            log.__dict__,
            ["comm_type", "comm_group"],
            ["count", "msg_size"],
            ["_elapsed_time"],
        )
        __update_info(
            detailed_comm_type_info,
            log.__dict__,
            ["comm_type", "comm_group", "msg_size"],
            ["count"],
            ["_elapsed_time"],
        )


class Log:
    def __init__(self) -> None:
        self.comm_logs = []
        self.comm_log_each_epoch = [[]]
        self.epoch_times = []

    def add_comm_log(self, comm_log: LogItem):
        if (
            comm_log.is_epoch_end()
            and len(self.comm_logs) > 0
            and not self.comm_logs[-1].is_epoch_end()
        ):
            self.comm_logs.append(comm_log)
            self.comm_log_each_epoch.append([])
            self.epoch_times.append(comm_log.elapsed_time)
            return
        self.comm_logs.append(comm_log)
        self.comm_log_each_epoch[-1].append(comm_log)

    def analyze(self, print_fn=print):
        comm_info: Dict[str, Dict] = {}
        _analyze_stage_log(self.comm_log_each_epoch[0], "init", comm_info)
        for e_log in self.comm_log_each_epoch[1:]:
            _analyze_stage_log(e_log, "train", comm_info)
        for stage in comm_info.keys():
            stage_count = comm_info[stage]["count"]
            comm_type_info = comm_info[stage]["comm_type_info"]
            detailed_comm_type_info = comm_info[stage]["detailed_comm_type_info"]
            print_fn(
                f"---------------------------------------------\ngeneral comm info for stage {stage}"
            )
            log_str = _print_stage_log(
                stage,
                stage_count,
                comm_type_info,
                ["comm_type", "comm_group"],
                ["count", "msg_size"],
                ["_elapsed_time"],
            )
            print_fn(log_str)

            print_fn(
                f"---------------------------------------------\ndetailed comm info for stage {stage}"
            )
            log_str = _print_stage_log(
                stage,
                stage_count,
                detailed_comm_type_info,
                ["comm_type", "comm_group", "msg_size"],
                ["count"],
                ["_elapsed_time"],
            )
            print_fn(log_str)
        return comm_info

    def dump(self, filename):
        default_comm_folder_path = "results/comm_logs/"
        if not os.path.exists(default_comm_folder_path):
            os.makedirs(default_comm_folder_path, exist_ok=True)
        if "." in filename:
            filename = filename.split(".")[0]
        filename = os.path.join("results/comm_logs/", filename)
        csv_filename = filename + "_log.csv"
        with open(csv_filename, "w") as f:
            f.write(self.comm_logs[0].csv_header() + "\n")
            for log_item in self.comm_logs:
                f.write(log_item.view_as_csv_line() + "\n")
        pkl_filename = filename + "_log.pkl"
        pickle.dump(self, open(pkl_filename, "wb"))

    @staticmethod
    def load(filename):
        filename = filename.split(".")
        filename[-1] = "pkl"
        filename = ".".join(filename)
        return pickle.load(open(filename, "rb"))

    def _get_elapsed_time(self):
        return self.epoch_times

    def analyze_time(self, print_fn=print):
        print_fn(f"--------------------------------------------------------")
        print_fn("result for epoch time ")
        print_fn(f"init time is {self.epoch_times[0]:.2f}")
        self.epoch_times.pop(0)
        max_val = max(self.epoch_times)
        min_val = min(self.epoch_times)
        mean_val = sum(self.epoch_times) / len(self.epoch_times)

        variance = sum((x - mean_val) ** 2 for x in self.epoch_times) / len(
            self.epoch_times
        )

        sorted_list = sorted(self.epoch_times)
        p90_val = sorted_list[int(len(sorted_list) * 0.9)]
        p99_val = sorted_list[int(len(sorted_list) * 0.99)]

        print_fn(f"max iteration time {max_val:.2f}")
        print_fn(f"min iteration time {min_val:.2f}")
        print_fn(f"avg iteration time {mean_val:.2f}")
        print_fn(f"p90 iteration time {p90_val:.2f}")
        print_fn(f"p99 iteration time {p99_val:.2f}")
        print_fn(f"iteration time variance {variance:.2f}")


class Workload:
    def __init__(self) -> None:
        self.workload = []

    def append(self, log_item: Union[LogItem, Dict]):
        if isinstance(log_item, LogItem):
            self.workload.append(log_item)
            return
        if "stage" not in log_item:
            log_item["stage"] = log_item["operation"] if "operation" in log_item else ""
        if "comm_group" not in log_item:
            assert (
                log_item["comm_type"] == CommType.computation
            ), "comm_group is required for non-computation comm_type"
            log_item["comm_group"] = CommGroup.all
        self.workload.append(
            LogItem(
                comm_type=log_item["comm_type"],
                comm_group=log_item["comm_group"],
                comm_group_size=log_item["comm_group_size"],
                msg_size=log_item["msg_size"],
                stage=log_item["stage"],
                src=log_item.get("src", None),
                dst=log_item.get("dst", None),
                additional=log_item.get("additional", None),
            )
        )

    def extend(self, new_workload):
        self.workload.extend(new_workload.workload)

    def dump(self, args, filename):
        folder_path = os.path.dirname(filename)
        if folder_path and not os.path.exists(folder_path):
            os.makedirs(folder_path)
        default_folder_path = "results/mocked_workload/"
        if not os.path.exists(default_folder_path):
            os.makedirs(default_folder_path, exist_ok=True)
        if "." in filename:
            filename = os.path.basename(filename).split(".")[0]
        filename = os.path.join("results/mocked_workload/", filename)
        pkl_filename = filename + "_workload.pkl"
        pickle.dump((self, args), open(pkl_filename, "wb"))
        csv_filename = filename + "_workload.csv"
        with open(csv_filename, "w") as f:
            f.write(self.workload[0].csv_header() + "\n")
            for log_item in self.workload:
                f.write(log_item.view_as_csv_line() + "\n")

    @staticmethod
    def load(filename):
        filename = filename.split(".")
        filename[-1] = "pkl"
        filename = ".".join(filename)
        workload, args = pickle.load(open(filename, "rb"))
        return workload, args
