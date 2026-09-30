#include "flexric_style2_action6_encoder.h"

#include "src/sm/rc_sm/ie/ir/ran_param_list.h"
#include "src/sm/rc_sm/ie/ir/ran_param_struct.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
  RRM_POLICY_RATIO_LIST = 1,
  RRM_POLICY = 3,
  RRM_POLICY_MEMBER_LIST = 5,
  PLMN_IDENTITY = 7,
  S_NSSAI = 8,
  SST = 9,
  SD = 10,
  MIN_PRB_POLICY_RATIO = 11,
  MAX_PRB_POLICY_RATIO = 12,
  DEDICATED_PRB_POLICY_RATIO = 13,
  ACTION_DL_MCS_BOUNDS = 101,
  ACTION_UE_DL_PRB_CAP = 102,
  ACTION_UE_PF_WEIGHT = 103,
  ACTION_CELL_DL_TX_POWER = 104,
  DL_MCS_BOUNDS = 201,
  DL_MCS_MAX = 202,
  DL_MCS_MIN = 203,
  UE_DL_PRB_CAP = 211,
  MAX_DL_PRBS = 212,
  UE_PF_WEIGHT = 221,
  PF_WEIGHT = 222,
  CELL_DL_TX_POWER = 231,
  DL_TX_ATTENUATION_DB = 232,
  TARGET_GNB_ID = 233,
};

static void failure(char *why, size_t why_len, const char *message)
{
  if (why != NULL && why_len != 0)
    snprintf(why, why_len, "%s", message);
}

static bool decimal_plmn(const slice_act_flexric_quota_t *quota)
{
  return quota->mcc <= 999 && quota->mnc <= 999
         && (quota->mnc_digit_len == 2 || quota->mnc_digit_len == 3)
         && (quota->mnc_digit_len == 3 || quota->mnc <= 99);
}

static void encode_plmn(const slice_act_flexric_quota_t *quota, uint8_t out[3])
{
  const uint8_t mcc1 = quota->mcc / 100;
  const uint8_t mcc2 = quota->mcc / 10 % 10;
  const uint8_t mcc3 = quota->mcc % 10;
  const uint8_t mnc1 = quota->mnc_digit_len == 2 ? quota->mnc / 10 : quota->mnc / 100;
  const uint8_t mnc2 = quota->mnc_digit_len == 2 ? quota->mnc % 10 : quota->mnc / 10 % 10;
  const uint8_t mnc3 = quota->mnc_digit_len == 2 ? 0x0f : quota->mnc % 10;
  out[0] = mcc2 << 4 | mcc1;
  out[1] = mnc3 << 4 | mcc3;
  out[2] = mnc2 << 4 | mnc1;
}

static bool set_octets(seq_ran_param_t *parameter,
                       uint32_t id,
                       const uint8_t *value,
                       size_t length)
{
  parameter->ran_param_id = id;
  parameter->ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  parameter->ran_param_val.flag_false = calloc(1, sizeof(*parameter->ran_param_val.flag_false));
  if (parameter->ran_param_val.flag_false == NULL)
    return false;
  parameter->ran_param_val.flag_false->type = OCTET_STRING_RAN_PARAMETER_VALUE;
  parameter->ran_param_val.flag_false->octet_str_ran.buf = malloc(length);
  if (parameter->ran_param_val.flag_false->octet_str_ran.buf == NULL)
    return false;
  memcpy(parameter->ran_param_val.flag_false->octet_str_ran.buf, value, length);
  parameter->ran_param_val.flag_false->octet_str_ran.len = length;
  return true;
}

static bool set_integer(seq_ran_param_t *parameter, uint32_t id, int64_t value)
{
  parameter->ran_param_id = id;
  parameter->ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  parameter->ran_param_val.flag_false = calloc(1, sizeof(*parameter->ran_param_val.flag_false));
  if (parameter->ran_param_val.flag_false == NULL)
    return false;
  parameter->ran_param_val.flag_false->type = INTEGER_RAN_PARAMETER_VALUE;
  parameter->ran_param_val.flag_false->int_ran = value;
  return true;
}

static bool set_real(seq_ran_param_t *parameter, uint32_t id, double value)
{
  parameter->ran_param_id = id;
  parameter->ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  parameter->ran_param_val.flag_false = calloc(1, sizeof(*parameter->ran_param_val.flag_false));
  if (parameter->ran_param_val.flag_false == NULL)
    return false;
  /* FlexRIC rc_enc_asn.c maps this exact IR discriminator/member pair to
   * RANParameter-Value.valueReal (ASN.1 NativeReal). */
  parameter->ran_param_val.flag_false->type = REAL_RAN_PARAMETER_VALUE;
  parameter->ran_param_val.flag_false->real_ran = value;
  return true;
}

static bool set_structure(seq_ran_param_t *parameter,
                          uint32_t id,
                          size_t count)
{
  parameter->ran_param_id = id;
  parameter->ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  parameter->ran_param_val.strct = calloc(1, sizeof(*parameter->ran_param_val.strct));
  if (parameter->ran_param_val.strct == NULL)
    return false;
  parameter->ran_param_val.strct->ran_param_struct = calloc(count, sizeof(seq_ran_param_t));
  if (parameter->ran_param_val.strct->ran_param_struct == NULL)
    return false;
  parameter->ran_param_val.strct->sz_ran_param_struct = count;
  return true;
}

static bool set_list(seq_ran_param_t *parameter, uint32_t id, size_t count)
{
  parameter->ran_param_id = id;
  parameter->ran_param_val.type = LIST_RAN_PARAMETER_VAL_TYPE;
  parameter->ran_param_val.lst = calloc(1, sizeof(*parameter->ran_param_val.lst));
  if (parameter->ran_param_val.lst == NULL)
    return false;
  parameter->ran_param_val.lst->lst_ran_param = calloc(count, sizeof(lst_ran_param_t));
  if (parameter->ran_param_val.lst->lst_ran_param == NULL)
    return false;
  parameter->ran_param_val.lst->sz_lst_ran_param = count;
  return true;
}

static bool build_group(const slice_act_flexric_quota_t *quota,
                        lst_ran_param_t *group)
{
  /* LIST item parameters 2 and 6 name the item definition.  ASN.1's
   * RANParameter-LIST carries only each item's RANParameter-STRUCTURE, so they
   * do not appear as seq_ran_param_t IDs on the wire. */
  group->ran_param_struct.ran_param_struct = calloc(4, sizeof(seq_ran_param_t));
  if (group->ran_param_struct.ran_param_struct == NULL)
    return false;
  group->ran_param_struct.sz_ran_param_struct = 4;
  seq_ran_param_t *children = group->ran_param_struct.ran_param_struct;

  if (!set_structure(&children[0], RRM_POLICY, 1))
    return false;
  seq_ran_param_t *member_list = children[0].ran_param_val.strct->ran_param_struct;
  if (!set_list(member_list, RRM_POLICY_MEMBER_LIST, 1))
    return false;

  ran_param_struct_t *member = &member_list->ran_param_val.lst->lst_ran_param[0].ran_param_struct;
  member->ran_param_struct = calloc(2, sizeof(seq_ran_param_t));
  if (member->ran_param_struct == NULL)
    return false;
  member->sz_ran_param_struct = 2;
  uint8_t plmn[3];
  encode_plmn(quota, plmn);
  if (!set_octets(&member->ran_param_struct[0], PLMN_IDENTITY, plmn, sizeof(plmn)))
    return false;

  const size_t snssai_count = quota->has_sd ? 2 : 1;
  if (!set_structure(&member->ran_param_struct[1], S_NSSAI, snssai_count))
    return false;
  seq_ran_param_t *snssai = member->ran_param_struct[1].ran_param_val.strct->ran_param_struct;
  if (!set_octets(&snssai[0], SST, &quota->sst, 1))
    return false;
  if (quota->has_sd) {
    uint8_t sd[3] = {quota->sd >> 16, quota->sd >> 8, quota->sd};
    if (!set_octets(&snssai[1], SD, sd, sizeof(sd)))
      return false;
  }

  return set_integer(&children[1], MIN_PRB_POLICY_RATIO, quota->min_prb_policy_ratio)
         && set_integer(&children[2], MAX_PRB_POLICY_RATIO, quota->max_prb_policy_ratio)
         && set_integer(&children[3], DEDICATED_PRB_POLICY_RATIO, quota->dedicated_prb_policy_ratio);
}

bool slice_act_build_flexric_style2_action6(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_quota_t *quotas,
    size_t quota_count,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len)
{
  if (out == NULL) {
    failure(why, why_len, "output request is NULL");
    return false;
  }
  memset(out, 0, sizeof(*out));
  if (header_ue_anchor == NULL || quotas == NULL || quota_count == 0) {
    failure(why, why_len, "Header Format 1 UE anchor and a non-empty RRM Policy Ratio List are mandatory");
    return false;
  }
  unsigned minimum_sum = 0;
  unsigned dedicated_sum = 0;
  for (size_t i = 0; i < quota_count; ++i) {
    const slice_act_flexric_quota_t *quota = &quotas[i];
    if (!decimal_plmn(quota) || quota->sst == 0 || (quota->has_sd && quota->sd > 0xffffff)) {
      failure(why, why_len, "PLMN or S-NSSAI is absent or malformed");
      return false;
    }
    for (size_t j = 0; j < i; ++j) {
      const slice_act_flexric_quota_t *prior = &quotas[j];
      if (quota->mcc == prior->mcc && quota->mnc == prior->mnc
          && quota->mnc_digit_len == prior->mnc_digit_len
          && quota->sst == prior->sst && quota->has_sd == prior->has_sd
          && (!quota->has_sd || quota->sd == prior->sd)) {
        failure(why, why_len, "duplicate PLMN/S-NSSAI policy member");
        return false;
      }
    }
    if (quota->dedicated_prb_policy_ratio > quota->min_prb_policy_ratio
        || quota->min_prb_policy_ratio > quota->max_prb_policy_ratio
        || quota->max_prb_policy_ratio > 100) {
      failure(why, why_len, "ratios must satisfy 0 <= dedicated <= minimum <= maximum <= 100");
      return false;
    }
    minimum_sum += quota->min_prb_policy_ratio;
    dedicated_sum += quota->dedicated_prb_policy_ratio;
  }
  if (minimum_sum > 100 || dedicated_sum > 100) {
    failure(why, why_len, "aggregate minimum/dedicated ratios exceed 100");
    return false;
  }

  out->hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  out->hdr.frmt_1.ue_id = cp_ue_id_e2sm(header_ue_anchor);
  out->hdr.frmt_1.ric_style_type = 2;
  out->hdr.frmt_1.ctrl_act_id = 6;
  out->hdr.frmt_1.ric_ctrl_decision = NULL;
  out->msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  out->msg.frmt_1.sz_ran_param = 1;
  out->msg.frmt_1.ran_param = calloc(1, sizeof(seq_ran_param_t));
  if (out->msg.frmt_1.ran_param == NULL
      || !set_list(out->msg.frmt_1.ran_param, RRM_POLICY_RATIO_LIST, quota_count)) {
    failure(why, why_len, "allocation failed while building RRM Policy Ratio List");
    free_rc_ctrl_req_data(out);
    memset(out, 0, sizeof(*out));
    return false;
  }
  for (size_t i = 0; i < quota_count; ++i) {
    if (!build_group(&quotas[i], &out->msg.frmt_1.ran_param->ran_param_val.lst->lst_ran_param[i])) {
      failure(why, why_len, "allocation failed while building RRM Policy Ratio Group");
      free_rc_ctrl_req_data(out);
      memset(out, 0, sizeof(*out));
      return false;
    }
  }
  return true;
}

static bool begin_single_structure(const ue_id_e2sm_t *header_ue_anchor,
                                   uint16_t action_id,
                                   uint32_t root_id,
                                   size_t leaf_count,
                                   rc_ctrl_req_data_t *out,
                                   char *why,
                                   size_t why_len)
{
  if (out == NULL) {
    failure(why, why_len, "output request is NULL");
    return false;
  }
  memset(out, 0, sizeof(*out));
  if (header_ue_anchor == NULL) {
    failure(why, why_len, "Control Header Format 1 UE anchor is mandatory");
    return false;
  }
  out->hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  out->hdr.frmt_1.ue_id = cp_ue_id_e2sm(header_ue_anchor);
  out->hdr.frmt_1.ric_style_type = 2;
  out->hdr.frmt_1.ctrl_act_id = action_id;
  out->hdr.frmt_1.ric_ctrl_decision = NULL;
  out->msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  out->msg.frmt_1.sz_ran_param = 1;
  out->msg.frmt_1.ran_param = calloc(1, sizeof(seq_ran_param_t));
  if (out->msg.frmt_1.ran_param == NULL
      || !set_structure(out->msg.frmt_1.ran_param, root_id, leaf_count)) {
    failure(why, why_len, "allocation failed while building local Action root STRUCTURE");
    free_rc_ctrl_req_data(out);
    memset(out, 0, sizeof(*out));
    return false;
  }
  return true;
}

static bool local_action_failure(rc_ctrl_req_data_t *out,
                                 char *why,
                                 size_t why_len)
{
  failure(why, why_len, "allocation failed while building local Action leaves");
  free_rc_ctrl_req_data(out);
  memset(out, 0, sizeof(*out));
  return false;
}

bool slice_act_build_flexric_style2_action101_dl_mcs_bounds(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_dl_mcs_bounds_t *bounds,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len)
{
  if (bounds == NULL || bounds->min_dl_mcs < 0 || bounds->max_dl_mcs > 28
      || bounds->min_dl_mcs > bounds->max_dl_mcs) {
    failure(why, why_len, "DL MCS bounds must satisfy 0 <= min <= max <= 28");
    return false;
  }
  if (!begin_single_structure(header_ue_anchor, ACTION_DL_MCS_BOUNDS,
                              DL_MCS_BOUNDS, 2, out, why, why_len))
    return false;
  seq_ran_param_t *leaves = out->msg.frmt_1.ran_param->ran_param_val.strct->ran_param_struct;
  /* Advertised definition order is maximum (202), then minimum (203). */
  if (!set_integer(&leaves[0], DL_MCS_MAX, bounds->max_dl_mcs)
      || !set_integer(&leaves[1], DL_MCS_MIN, bounds->min_dl_mcs))
    return local_action_failure(out, why, why_len);
  return true;
}

bool slice_act_build_flexric_style2_action102_ue_dl_prb_cap(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_ue_dl_prb_cap_t *cap,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len)
{
  if (cap == NULL || cap->max_dl_prbs < 0 || cap->max_dl_prbs > 275) {
    failure(why, why_len, "Maximum DL PRBs must be an integer in 0..275");
    return false;
  }
  if (!begin_single_structure(header_ue_anchor, ACTION_UE_DL_PRB_CAP,
                              UE_DL_PRB_CAP, 1, out, why, why_len))
    return false;
  seq_ran_param_t *leaf = out->msg.frmt_1.ran_param->ran_param_val.strct->ran_param_struct;
  if (!set_integer(leaf, MAX_DL_PRBS, cap->max_dl_prbs))
    return local_action_failure(out, why, why_len);
  return true;
}

bool slice_act_build_flexric_style2_action103_ue_pf_weight(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_ue_pf_weight_t *priority,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len)
{
  if (priority == NULL || !isfinite(priority->pf_weight)
      || priority->pf_weight < 0.001 || priority->pf_weight > 100.0) {
    failure(why, why_len, "PF Weight must be a finite REAL in 0.001..100");
    return false;
  }
  if (!begin_single_structure(header_ue_anchor, ACTION_UE_PF_WEIGHT,
                              UE_PF_WEIGHT, 1, out, why, why_len))
    return false;
  seq_ran_param_t *leaf = out->msg.frmt_1.ran_param->ran_param_val.strct->ran_param_struct;
  if (!set_real(leaf, PF_WEIGHT, priority->pf_weight))
    return local_action_failure(out, why, why_len);
  return true;
}

bool slice_act_build_flexric_style2_action104_cell_dl_tx_power(
    const ue_id_e2sm_t *header_ue_anchor,
    const slice_act_flexric_cell_dl_tx_power_t *power,
    rc_ctrl_req_data_t *out,
    char *why,
    size_t why_len)
{
  if (power == NULL || !isfinite(power->tx_attenuation_db)
      || power->target_gnb_id < 0 || (uint64_t)power->target_gnb_id > UINT32_MAX) {
    failure(why, why_len, "TX attenuation must be finite and Target gNB ID must fit uint32");
    return false;
  }
  if (!begin_single_structure(header_ue_anchor, ACTION_CELL_DL_TX_POWER,
                              CELL_DL_TX_POWER, 2, out, why, why_len))
    return false;
  seq_ran_param_t *leaves = out->msg.frmt_1.ran_param->ran_param_val.strct->ran_param_struct;
  if (!set_real(&leaves[0], DL_TX_ATTENUATION_DB, power->tx_attenuation_db)
      || !set_integer(&leaves[1], TARGET_GNB_ID, power->target_gnb_id))
    return local_action_failure(out, why, why_len);
  return true;
}
