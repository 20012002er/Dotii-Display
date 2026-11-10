#ifndef WINDOW_WORLD_H
#define WINDOW_WORLD_H

#include "app_state.h"
#include "lvgl.h"

/* 窗外小世界：圆屏变成一扇窗。
   本地时间驱动日出日落，天气快照驱动雨雪云雾；
   Codex 工作时亮起跑腿小人，Bambu 打印时远处工厂冒烟。
   全部图形本地绘制，离线时保留昼夜变化。 */
lv_obj_t *window_world_build(void);
void window_world_apply(const codex_snapshot_t *snapshot);

#endif
