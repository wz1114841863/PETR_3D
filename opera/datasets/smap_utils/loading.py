# 读取数据
import os.path as osp
import mmcv
import numpy as np
from mmdet.datasets.pipelines import LoadAnnotations as MMDetLoadAnnotations

from ..builder import PIPELINES

@PIPELINES.register_module()
class LoadImgFromFile:
    """load an image form file
        参考mmdet.datasets.pipelines.loading.py
    """
    def __init__(self,
                    to_float32=False,
                    color_type='color',
                    channel_order='bgr',
                    file_client_args=dict(backend='disk')):
        self.to_float32 = to_float32
        self.color_type = color_type
        self.channel_order = channel_order
        self.file_client_args = file_client_args.copy()
        self.file_client = None
    
    def __call__(self, results):
        """这里的results满足SMAP定义的格式
        存在两种情况：
            COCO:
            MuCo:
        Return:
            results (dict): JointDatase 传递的字典。
            ['ann_info', 'img_prefix', 'seg_prefix', 
            'proposal_file', 'bbox_fields', 'mask_fields', 
            'keypoint_fields', 'filename', 'ori_filename', 'img', 
            'img_shape', 'ori_shape', 'img_fields']
        """
        if self.file_client is None:
            self.file_client = mmcv.FileClient(**self.file_client_args)
        
        if results['img_prefix'] is not None:
            # 注意这里对应的img_prefix是不同数据集自己的路径。
            filepath = osp.join(results['img_prefix'], 
                                results['ann_info']['img_paths'])
        else:
            raise ValueError("img_prefix 不能为空。")
        
        img_bytes = self.file_client.get(filepath=filepath)
        img = mmcv.imfrombytes(
            img_bytes, flag=self.color_type, channel_order=self.channel_order)
        
        if self.to_float32:
            img = img.astype(np.float32)
        
        results['filename'] = filepath
        results['ori_filename'] = results['ann_info']['img_paths']
        results['img'] = img
        results['img_shape'] = img.shape
        results['ori_shape'] = img.shape
        results['img_fields'] = ['img']
        return results
    
    def __repr__(self):
        repr_str = (f'{self.__class__.__name__}('
                f'to_float32={self.to_float32}, '
                f"color_type='{self.color_type}', "
                f"channel_order='{self.channel_order}', "
                f'file_client_args={self.file_client_args})')
        return repr_str


@PIPELINES.register_module()
class LoadAnnosFromFile(MMDetLoadAnnotations):
    """对注释文件进行进一步解析,转换格式，添加字段。
    Args:
        with_dataset:(bool):
        with_bbox:(bool):
    Return:
        add new keys:
            ['gt_bboxs', 'dataset', 'gt_keypoints', 'keypoints_fields', ['gt_keypoints_flag']]
    """
    def __init__(self, 
                *args,
                with_dataset=True, 
                with_keypoints=True,
                **kwargs):
        super(LoadAnnosFromFile, self).__init__(*args, **kwargs)
        self.with_dataset = with_dataset
        self.with_keypoints = with_keypoints
    
    def _load_bboxes(self, results):
        bboxs = results['ann_info']['bboxs'].copy()
        bboxs = np.asarray(bboxs)
        # 修改bboxs，coco中的格式为：[x1, y1, w, h]
        # 注意这里仅修改了results['bboxs']
        bboxs[:, 2] += bboxs[:, 0]
        bboxs[:, 3] += bboxs[:, 1]
        # 确保bbox中的坐标为[left_top_x, left_top_y, right_bottom_x, right_bottom_y]
        for i in range(len(bboxs)):
            left_top_x = min(bboxs[i][0], bboxs[i][2])
            left_top_y = min(bboxs[i][1], bboxs[i][3])
            right_bottom_x = max(bboxs[i][0], bboxs[i][2])
            right_bottom_y = max(bboxs[i][1], bboxs[i][3])
            bbox = [left_top_x, left_top_y, right_bottom_x, right_bottom_y]
            bboxs[i] = bbox
        results['gt_bboxs'] = bboxs
        results['bbox_fields'] = ['gt_bboxs']
        return results
    
    def _load_dataset(self, results):
        results['dataset'] = results['ann_info']['dataset'].upper()
        return results
    
    def _load_keypoints(self, results):
        """加载关键点的同时添加关键点标志位
        确保原来无效的坐标点，经过数据增强后依旧是(0, 0, ...)
        """
        # 加载关键点数据
        keypoints = results['ann_info']['bodys'].copy()  # [N, J, 11]
        results['gt_keypoints'] = np.asarray(keypoints)
        results['keypoint_fields'] = ['gt_keypoints']
        # 添加keyPoints标志位
        keypoints_flag = np.ones((len(keypoints), 15, 1), dtype=int)
        for n in range(len(keypoints)):
            for j in range(15):
                if keypoints[n][j][0] == 0.0 or keypoints[n][j][1] == 0.0:
                    keypoints_flag[n][j][0] = 0
                    
        results['gt_keypoints_flag'] = keypoints_flag
        return results
    
    def __call__(self, results):
        results = super(LoadAnnosFromFile, self).__call__(results)
        
        if results is None:
            return None
        
        if self.with_dataset:
            results = self._load_dataset(results)
            
        if self.with_keypoints:
            results = self._load_keypoints(results)
            
        return results
    
    def __repr__(self):
        repr_str = super(LoadAnnosFromFile, self).__repr__()[:-1] + ', '
        repr_str += f'with_dataset={self.with_dataset}, '
        repr_str += f'with_keypoint={self.with_keypoints}, '
        return repr_str
