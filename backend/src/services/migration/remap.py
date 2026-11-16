"""按目标库实际主键重建运行结果中的全部窗口引用，源库数字 id 一律不沿用。"""
import copy


def remap_run_result(source_result: dict, window_id_map: dict[int, int]) -> dict:
    """window_id_map: 源窗口 id -> 目标窗口 id（按 reference 建立）。"""
    result = copy.deepcopy(source_result)
    result['split_ids'] = {
        group: [window_id_map[i] for i in ids]
        for group, ids in source_result['split_ids'].items()
    }
    result['test_predictions'] = [
        {
            **p,
            'window_id': window_id_map[p['window_id']],
        }
        for p in source_result['test_predictions']
    ]
    return result
