import csv
import os
import time

import numpy as np
import sklearn.metrics
import torch.nn as nn
import torch.utils.data
import tqdm

import utils


class EvaluationMetrics:
    def __init__(self, num_classes: int):
        self.labels = list(range(num_classes))
        self.confusion_matrix = np.zeros((num_classes, num_classes))

    def update_matrix(self, gt_batch: torch.Tensor, pred_batch: torch.Tensor):
        assert gt_batch.shape[0] == pred_batch.shape[0]

        gt = torch.flatten(gt_batch, start_dim=1).cpu().numpy()
        pred = torch.flatten(pred_batch, start_dim=1).cpu().numpy()

        # 1 배치단위 처리
        for i in range(gt_batch.shape[0]):
            self.confusion_matrix += sklearn.metrics.confusion_matrix(gt[i], pred[i], labels=self.labels)

    def get_scores(self, ignore_first_label=False, ignore_last_label=False):
        if ignore_first_label:
            self.confusion_matrix = self.confusion_matrix[1:, 1:]
        if ignore_last_label:
            last_label = self.confusion_matrix.shape[0] - 1
            self.confusion_matrix = self.confusion_matrix[:last_label, :last_label]

        iou = np.diag(self.confusion_matrix) / (self.confusion_matrix.sum(axis=0) +
                                                self.confusion_matrix.sum(axis=1) -
                                                np.diag(self.confusion_matrix)) * 100
        miou = np.mean(iou)
        return iou, miou


def evaluate(model, testloader, criterion, num_classes: int, amp_enabled: bool, device, eval_fps=True):
    model.eval()

    # Evaluate
    metrics = EvaluationMetrics(num_classes)
    val_loss = 0
    inference_time = 0
    for image, target in tqdm.tqdm(testloader, desc='Eval', leave=False):
        # target의 정규화를 해제 (0~1 값을 0~255 값으로 변경) + 채널 차원 제거
        target.mul_(255).squeeze_(dim=1)
        image, target = image.to(device), target.to(device, dtype=torch.int64)

        # 예측
        with torch.cuda.amp.autocast(enabled=amp_enabled):
            if eval_fps:
                torch.cuda.synchronize()
                start_time = time.time()
                with torch.no_grad():
                    output = model(image)
                torch.cuda.synchronize()
                inference_time += time.time() - start_time
            else:
                with torch.no_grad():
                    output = model(image)

            # validation loss를 모두 합침
            val_loss += criterion(output, target).item()

            # Segmentation map 만들기
            output = torch.argmax(output, dim=1)

        # 혼동행렬 업데이트
        metrics.update_matrix(target, output)

    # 평가 점수 가져오기
    iou, miou = metrics.get_scores(ignore_first_label=True)

    # 평균 validation loss 계산
    val_loss /= len(testloader)

    # 추론 시간과 fps를 계산 (추론 시간 단위: sec)
    if eval_fps:
        inference_time /= len(testloader.dataset)
        fps = 1 / inference_time
    else:
        fps = 0

    return val_loss, iou, miou, fps


if __name__ == '__main__':
    # 0. Load config
    config = utils.load_config()
    print('Activated model: {}'.format(config['model']))

    # 1. Dataset
    dataset = utils.Cityscapes(config)
    _, _, _, testloader = dataset.set_cityscapes()

    # 2. Model
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = utils.get_model(config, pretrained=True).to(device)
    model.eval()

    # 3. Loss function
    criterion = nn.CrossEntropyLoss()

    # 모델 평가
    val_loss, iou, miou, fps = evaluate(model, testloader, criterion, config['dataset']['num_classes'],
                                        config['amp_enabled'], device)

    # 평가 결과를 csv 파일로 저장
    os.makedirs('result', exist_ok=True)
    class_names = dataset.get_class_names()
    with open(os.path.join('result', '{}.csv'.format(config['model'])), mode='w') as f:
        writer = csv.writer(f, delimiter=',', lineterminator='\n')

        writer.writerow(['Class Number', 'Class Name', 'IoU'])
        for class_num, iou_value in enumerate(iou, start=1):
            writer.writerow([class_num, class_names[class_num], iou_value])
        writer.writerow(['mIoU', miou, ' '])
        writer.writerow(['Validation loss', val_loss, ' '])
        writer.writerow(['FPS', fps, ' '])
    print('Saved evaluation result.')
