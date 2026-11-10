#ifndef PERFORMANCE_MONITOR_H
#define PERFORMANCE_MONITOR_H

#include "app_state.h"
#include "lvgl.h"

lv_obj_t *performance_monitor_build(void);
lv_obj_t *performance_monitor_content(void);
void performance_monitor_apply(const codex_snapshot_t *snapshot);

#endif
