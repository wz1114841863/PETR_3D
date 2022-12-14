# Copyright (c) Hikvision Research Institute. All rights reserved.
import torch
from mmdet.core.bbox.assigners.assign_result import AssignResult
from mmdet.core.bbox.assigners.base_assigner import BaseAssigner

from opera.core.bbox.builder import BBOX_ASSIGNERS
from opera.core.bbox.match_costs import build_match_cost

try:
    from scipy.optimize import linear_sum_assignment
except ImportError:
    linear_sum_assignment = None


@BBOX_ASSIGNERS.register_module()
class PoseHungarianAssigner3D(BaseAssigner):
    """Computes one-to-one matching between predictions and ground truth.

    This class computes an assignment between the targets and the predictions
    based on the costs. The costs are weighted sum of three components:
    classification cost, regression L1 cost and regression oks cost. The
    targets don't include the no_object, so generally there are more
    predictions than targets. After the one-to-one matching, the un-matched
    are treated as backgrounds. Thus each query prediction will be assigned
    with `0` or a positive integer indicating the ground truth index:

    - 0: negative sample, no assigned gt.
    - positive integer: positive sample, index (1-based) of assigned gt.

    Args:
        cls_weight (int | float, optional): The scale factor for classification
            cost. Default 1.0.
        kpt_weight (int | float, optional): The scale factor for regression
            L1 cost. Default 1.0.
        oks_weight (int | float, optional): The scale factor for regression
            oks cost. Default 1.0.
    """

    def __init__(self,
                    cls_cost=dict(type='ClassificationCost', weight=1.0),
                    kpt_cost=dict(type='KptL1Cost', weight=1.0),
                    oks_cost=dict(type='OksCost', weight=1.0),
                    depth_cost=dict(type='DepthL1Cost', weight=1.0)):
        self.cls_cost = build_match_cost(cls_cost)
        self.kpt_cost = build_match_cost(kpt_cost)
        self.oks_cost = build_match_cost(oks_cost)
        self.depth_cost = build_match_cost(depth_cost)

    def assign(self,
                cls_pred,
                kpt_pred,
                depth_pred,
                gt_labels,
                gt_keypoints,
                gt_areas,
                dataset,
                img_meta,
                eps=1e-7):
        """Computes one-to-one matching based on the weighted costs.

        This method assign each query prediction to a ground truth or
        background. The `assigned_gt_inds` with -1 means don't care,
        0 means negative sample, and positive number is the index (1-based)
        of assigned gt.
        The assignment is done in the following steps, the order matters.

        1. assign every prediction to -1
        2. compute the weighted costs
        3. do Hungarian matching on CPU based on the costs
        4. assign all to 0 (background) first, then for each matched pair
            between predictions and gts, treat this prediction as foreground
            and assign the corresponding gt index (plus 1) to it.

        Args:
            cls_pred (Tensor): Predicted classification logits, shape
                [num_query, num_class].
            kpt_pred (Tensor): Predicted keypoints with normalized coordinates
                (x_{i}, y_{i}), which are all in range [0, 1], reference point depth
                and keypoints releative depth Shape [num_query, K*2].
            kpt_depth:reference point depth and keypoints releative depth.
            gt_labels (Tensor): Label of `gt_keypoints`, shape (num_gt,).
            gt_keypoints (Tensor): Ground truth keypoints with unnormalized
                coordinates and depth. Shape [num_gt, 15, 11].
            gt_areas (Tensor): Ground truth mask areas, shape (num_gt,).
            img_meta (dict): Meta information for current image.
            eps (int | float, optional): A value added to the denominator for
                numerical stability. Default 1e-7.

        Returns:
            :obj:`AssignResult`: The assigned result.
        """
        num_gts, num_kpts = gt_keypoints.size(0), kpt_pred.size(0)  # gt?????? ??? ????????????

        # 1. assign -1 by default
        assigned_gt_inds = kpt_pred.new_full((num_kpts, ),
                                        -1, dtype=torch.long)  # 300
        assigned_labels = kpt_pred.new_full((num_kpts, ),
                                        -1, dtype=torch.long)  # 300
        
        if num_gts == 0 or num_kpts == 0:
            # No ground truth or keypoints, return empty assignment
            if num_gts == 0:
                # No ground truth, assign all to background
                assigned_gt_inds[:] = 0
            return AssignResult(
                num_gts, assigned_gt_inds, None, labels=assigned_labels)
        
        # factor    
        img_h, img_w, _ = img_meta['img_shape']
        factor = gt_keypoints.new_tensor([img_w, img_h, img_w,
                                            img_h]).unsqueeze(0)  # [1, 4]
        # 2. compute the weighted costs
        # classification cost
        cls_cost = self.cls_cost(cls_pred, gt_labels)  # [300, num_gts]

        # keypoint regression L1 cost
        gt_keypoints_reshape = gt_keypoints[..., :2]  # [num_gts, 15, 2], (x,y)
        valid_kpt_flag = gt_keypoints[..., 3]  # [num_gts, 15], vis
        kpt_pred_tmp = kpt_pred.clone().detach().reshape(
            kpt_pred.shape[0], -1, 2)  # [300, 15, 2]
        normalize_gt_keypoints = gt_keypoints_reshape[
            ..., :2] / factor[:, :2].unsqueeze(0)  # [num_gts, 15, 2]
        kpt_cost = self.kpt_cost(kpt_pred_tmp, normalize_gt_keypoints,
                                    valid_kpt_flag)  # [300, num_kpt]
        
        # keypoint OKS cost
        # FIXME ?????????oks_cost????????????cost???????????????????????????
        kpt_pred_tmp = kpt_pred.clone().detach().reshape(
            kpt_pred.shape[0], -1, 2)  # [300, 15, 2]
        kpt_pred_tmp = kpt_pred_tmp * factor[:, :2].unsqueeze(0)  # ????????????????????? * ????????????
        oks_cost = self.oks_cost(kpt_pred_tmp, gt_keypoints_reshape[..., :2],
                                    valid_kpt_flag, gt_areas)  # [300, num_kpt]
        # keypoint Depth cost
        if dataset == "COCO":
            refer_depth_cost = 0 
            kpt_depth_cost = 0
        elif dataset == "MUCO":
            gt_keypoints_depth = gt_keypoints[..., -5:]  # [numt_gts, 15, 5] ,[Z, fx, fy, cx, cy]
            depth_pred_tmp = depth_pred.clone().detach() # [300, 1 + 15], ?????????????????????
            refer_point_depth = depth_pred_tmp[..., 0][..., None]  # [300, 1], reference point ????????????
            kpt_real_depth = depth_pred_tmp[..., 1:]  # [300, 15], keypoints ????????????
            kpt_abs_depth = kpt_real_depth + refer_point_depth  # [300, 15], keypoints ????????????
        
            refer_depth_cost, kpt_depth_cost = self.depth_cost(refer_point_depth, kpt_abs_depth, 
                gt_keypoints_depth, valid_kpt_flag, img_meta['scale_factor'])  # ???????????? [300, num_kpt]
        else:
            raise NotImplementedError("?????????dataset in hungarisn_assigner.")
        
        # weighted sum of above three costs
        cost = cls_cost + kpt_cost + oks_cost + refer_depth_cost + kpt_depth_cost  # [300, num_kpt]

        # 3. do Hungarian matching on CPU using linear_sum_assignment
        cost = cost.detach().cpu()
        if linear_sum_assignment is None:
            raise ImportError('Please run "pip install scipy" '
                                'to install scipy first.')
        try:
            matched_row_inds, matched_col_inds = linear_sum_assignment(cost)  # [num_gts, ]
        except:
            print('Error in linear_sum_assignment')
            import pdb;pdb.set_trace()
        matched_row_inds = torch.from_numpy(matched_row_inds).to(
            kpt_pred.device)
        matched_col_inds = torch.from_numpy(matched_col_inds).to(
            kpt_pred.device)

        # 4. assign backgrounds and foregrounds
        # assign all indices to backgrounds first
        assigned_gt_inds[:] = 0
        # assign foregrounds based on matching results
        assigned_gt_inds[matched_row_inds] = matched_col_inds + 1
        assigned_labels[matched_row_inds] = gt_labels[matched_col_inds]
        return AssignResult(
            num_gts, assigned_gt_inds, None, labels=assigned_labels)
