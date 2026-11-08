import os

import torch
import yaml
from ultralytics import YOLO
from pathlib import Path


def abs_path(rel_path, path_type=None):
    """替代原来的 QtFusion.path.abs_path（本脚本在项目根，两种取法结果一致）。"""
    return str(Path(__file__).resolve().parent / rel_path)

device = "0" if torch.cuda.is_available() else "cpu"

if __name__ == '__main__':
    workers = 4
    batch = 8

    data_name = "InsectPest"
    data_path = abs_path(f'datasets/{data_name}/{data_name}.yaml', path_type='current')
    unix_style_path = data_path.replace(os.sep, '/')

    directory_path = os.path.dirname(unix_style_path)
    with open(data_path, 'r') as file:
        data = yaml.load(file, Loader=yaml.FullLoader)
    if 'path' in data:
        data['path'] = directory_path
        with open(data_path, 'w') as file:
            yaml.safe_dump(data, file, sort_keys=False)

    # ========== YOLO11s 训练 ==========
    # YOLO11 是 ultralytics 最新架构，精度速度均优于 YOLOv8
    # 首次运行会自动下载 yolo11s.pt 预训练权重
    model = YOLO('yolo11s.pt', task='detect')
    results = model.train(
        data=data_path,
        device=device,
        workers=workers,
        imgsz=640,
        epochs=150,
        batch=batch,
        name='train_yolo11s_' + data_name,
        patience=30,  # early stopping，30轮不提升则自动停止
    )
    print("YOLO11s 训练完成！")
