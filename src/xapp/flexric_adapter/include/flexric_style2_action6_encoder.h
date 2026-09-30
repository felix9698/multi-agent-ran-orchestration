#pragma once

#include "src/sm/rc_sm/ie/rc_data_ie.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint16_t mcc;
  uint16_t mnc;
  uint8_t mnc_digit_len;
  uint8_t sst;
  bool has_sd;
  uint32_t sd;
  uint8_t min_prb_policy_ratio;
  uint8_t max_prb_policy_ratio;
  uint8_t dedicated_prb_policy_ratio;
} slice_act_flexric_quota_t;

typedef struct {
  int64_t min_dl_mcs;
  int64_t max_dl_mcs;
} slice_act_flexric_dl_mcs_bounds_t;

typedef struct {
  int64_t max_dl_prbs;
} slice_act_flexric_ue_dl_prb_cap_t;

typedef struct {
  double pf_weight;
} slice_act_flexric_ue_pf_weight_t;

typedef struct {
  double tx_attenuation_db;
  int64_t target_gnb_id;
} slice_act_flexric_cell_dl_tx_power_t;

/**
 * Construct an owning FlexRIC rc_ctrl_req_data_t for E2SM-RC v1.03 Control
 * Header Format 1 and Control Message Format 1, Style 2 / Action 6.
 *
 * Header Format 1 requires a UE ID in the pinned FlexRIC IR, so the caller must
 * supply the already-resolved E2 node anchor.  The encoder copies it.  The
 * returned request is released with free_rc_ctrl_req_data().
 */
bool slice_act_build_flexric_style2_action6(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_quota_t *quotas,
    size_t quota_count,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len);

/** Build deployment-local Style 2 Actions 101--104.  Each request owns one
 * root RANParameter-STRUCTURE and is released with free_rc_ctrl_req_data(). */
bool slice_act_build_flexric_style2_action101_dl_mcs_bounds(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_dl_mcs_bounds_t *bounds,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len);

bool slice_act_build_flexric_style2_action102_ue_dl_prb_cap(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_ue_dl_prb_cap_t *cap,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len);

bool slice_act_build_flexric_style2_action103_ue_pf_weight(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_ue_pf_weight_t *priority,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len);

bool slice_act_build_flexric_style2_action104_cell_dl_tx_power(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_cell_dl_tx_power_t *power,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len);

#ifdef __cplusplus
}
#endif
