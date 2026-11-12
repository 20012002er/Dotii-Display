#ifndef TURNTABLE_H
#define TURNTABLE_H

#include "app_state.h"
#include "lvgl.h"

lv_obj_t *turntable_build(void);
lv_obj_t *turntable_content(void);
void turntable_apply(const codex_snapshot_t *snapshot);
void turntable_tick(void);

#endif
