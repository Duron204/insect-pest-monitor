import os

import torch
import yaml
from ultralytics import YOLO  # 导入YOLO模型
from pathlib import Path


def abs_path(rel_path, path_type=None):
    """替代原来的 QtFusion.path.abs_path。

    本脚本位于项目根目录，因此 QtFusion 的两种取法（path_type='current' 按调用者
    文件所在目录、省略则按项目根）在这里结果完全一致，统一按「脚本所在目录」解析。
    这样训练脚本不再依赖 QtFusion，桌面端技术栈才能真正移除。
    """
    return str(Path(__file__).resolve().parent / rel_path)
device = "0" if torch.cuda.is_available() else "cpu"

if __name__ == '__main__':  # 确保该模块被直接运行时才执行以下代码
    workers = 1
    batch = 8

    data_name = "InsectPest"
    data_path = abs_path(f'datasets/{data_name}/{data_name}.yaml', path_type='current')  # 数据集的yaml的绝对路径
    unix_style_path = data_path.replace(os.sep, '/')

    # 获取目录路径
    directory_path = os.path.dirname(unix_style_path)
    # 读取YAML文件，保持原有顺序
    with open(data_path, 'r') as file:
        data = yaml.load(file, Loader=yaml.FullLoader)
    # 修改path项
    if 'path' in data:
        data['path'] = directory_path
        # 将修改后的数据写回YAML文件
        with open(data_path, 'w') as file:
            yaml.safe_dump(data, file, sort_keys=False)

    model = YOLO('yolov8n.pt', task='detect')  # 加载预训练的YOLOv8模型
    results2 = model.train(  # 开始训练模型
        data=data_path,  # 指定训练数据的配置文件路径
        device=device,  # 自动选择进行训练
        workers=workers,  # 指定使用2个工作进程加载数据
        imgsz=640,  # 指定输入图像的大小为640x640
        epochs=120,  # 指定训练100个epoch
        batch=batch,  # 指定每个批次的大小为8
        name='train_v8_' + data_name  # 指定训练任务的名称
    )

    model = YOLO('yolov5nu.pt', task='detect')  # 加载预训练的YOLOv8模型
    # model = YOLO('yolov5.yaml', task='detect').load('yolov5nu.pt')  # 加载预训练的YOLOv8模型
    # Training.
    results = model.train(  # 开始训练模型
        data=data_path,  # 指定训练数据的配置文件路径
        device=device,  # 自动选择进行训练
        workers=workers,  # 指定使用2个工作进程加载数据
        imgsz=640,  # 指定输入图像的大小为640x640
        epochs=120,  # 指定训练100个epoch
        batch=batch,  # 指定每个批次的大小为8
        name='train_v5_' + data_name  # 指定训练任务的名称
    )
