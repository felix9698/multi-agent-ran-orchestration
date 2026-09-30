#include "flexric_control_adapter.h"

#include <limits>
#include <stdexcept>

namespace oran_aic::slice_act {
namespace {

uint16_t Decimal(const std::string& value) {
  const unsigned long parsed = std::stoul(value);
  if (parsed > std::numeric_limits<uint16_t>::max()) {
    throw std::invalid_argument("PLMN component exceeds native range");
  }
  return static_cast<uint16_t>(parsed);
}

const RanParameter& SingleRoot(const RcControlRequest& request,
                               std::int64_t expected_root,
                               std::size_t expected_leaves) {
  if (request.header_format != 1 || request.style_type != 2 ||
      request.message_format != 1 || request.ue_anchor_ref.empty() ||
      request.ran_parameters.size() != 1) {
    throw std::invalid_argument("logical request is not Style 2 HF1/MF1 with one root");
  }
  const RanParameter& root = request.ran_parameters.front();
  if (root.id != expected_root || root.type != RanValueType::kStructure ||
      root.children.size() != expected_leaves) {
    throw std::invalid_argument("logical request does not match the advertised root STRUCTURE");
  }
  return root;
}

void RequireLeaf(const RanParameter& leaf, std::int64_t id, RanValueType type) {
  if (leaf.id != id || leaf.type != type || !leaf.children.empty()) {
    throw std::invalid_argument("logical request leaf ID/type does not match the advertised definition");
  }
}

bool SubmitOwned(FlexricControlApi& api,
                 rc_ctrl_req_data_t& native,
                 const std::string& scope_key,
                 std::uint64_t fencing_token,
                 const std::string& idempotency_key) {
  try {
    const bool acknowledged = api.WriteControl(
        native, scope_key, fencing_token, idempotency_key);
    free_rc_ctrl_req_data(&native);
    return acknowledged;
  } catch (...) {
    free_rc_ctrl_req_data(&native);
    throw;
  }
}

}  // namespace

bool NativeFlexricRcTransport::Send(const std::string& scope_key,
                                    std::uint64_t fencing_token,
                                    const std::string& idempotency_key,
                                    const SlicePrbRequest& source,
                                    const RcControlRequest& request) {
  if (request.header_format != 1 || request.style_type != 2 ||
      request.action_id != 6 || request.message_format != 1 ||
      request.ue_anchor_ref != source.ue_anchor_ref) {
    throw std::invalid_argument("logical request is not Style 2 / Action 6 with the resolved UE anchor");
  }
  if (!source.snssai.has_value()) {
    throw std::invalid_argument("S-NSSAI is mandatory at the native FlexRIC boundary");
  }
  const ue_id_e2sm_t* anchor = resolver_.Resolve(request.ue_anchor_ref);
  if (anchor == nullptr) {
    throw std::invalid_argument("UE anchor reference cannot be resolved to a FlexRIC UE ID");
  }

  const SNssai& snssai = *source.snssai;
  slice_act_flexric_quota_t quota = {};
  quota.mcc = Decimal(source.plmn.mcc);
  quota.mnc = Decimal(source.plmn.mnc);
  quota.mnc_digit_len = static_cast<uint8_t>(source.plmn.mnc.size());
  quota.sst = snssai.sst;
  quota.has_sd = snssai.sd.has_value();
  quota.sd = snssai.sd.value_or(0);
  quota.min_prb_policy_ratio = static_cast<uint8_t>(source.ratios.minimum);
  quota.max_prb_policy_ratio = static_cast<uint8_t>(source.ratios.maximum);
  quota.dedicated_prb_policy_ratio = static_cast<uint8_t>(source.ratios.dedicated);
  rc_ctrl_req_data_t native = {};
  char why[192] = {};
  if (!slice_act_build_flexric_style2_action6(anchor, &quota, 1,
                                               &native, why, sizeof(why))) {
    throw std::invalid_argument(why);
  }
  return SubmitOwned(api_, native, scope_key, fencing_token, idempotency_key);
}

bool NativeFlexricRcTransport::WriteControl(
    const std::string& scope_key,
    std::uint64_t fencing_token,
    const std::string& idempotency_key,
    const RcControlRequest& request) {
  const ue_id_e2sm_t* anchor = resolver_.Resolve(request.ue_anchor_ref);
  if (anchor == nullptr) {
    throw std::invalid_argument("UE anchor reference cannot be resolved to a FlexRIC UE ID");
  }

  rc_ctrl_req_data_t native = {};
  char why[192] = {};
  bool built = false;
  switch (request.action_id) {
    case 101: {
      const RanParameter& root = SingleRoot(request, 201, 2);
      RequireLeaf(root.children[0], 202, RanValueType::kElementInteger);
      RequireLeaf(root.children[1], 203, RanValueType::kElementInteger);
      const slice_act_flexric_dl_mcs_bounds_t bounds = {
          root.children[1].integer, root.children[0].integer};
      built = slice_act_build_flexric_style2_action101_dl_mcs_bounds(
          anchor, &bounds, &native, why, sizeof(why));
      break;
    }
    case 102: {
      const RanParameter& root = SingleRoot(request, 211, 1);
      RequireLeaf(root.children[0], 212, RanValueType::kElementInteger);
      const slice_act_flexric_ue_dl_prb_cap_t cap = {root.children[0].integer};
      built = slice_act_build_flexric_style2_action102_ue_dl_prb_cap(
          anchor, &cap, &native, why, sizeof(why));
      break;
    }
    case 103: {
      const RanParameter& root = SingleRoot(request, 221, 1);
      RequireLeaf(root.children[0], 222, RanValueType::kElementReal);
      const slice_act_flexric_ue_pf_weight_t priority = {root.children[0].real};
      built = slice_act_build_flexric_style2_action103_ue_pf_weight(
          anchor, &priority, &native, why, sizeof(why));
      break;
    }
    case 104: {
      const RanParameter& root = SingleRoot(request, 231, 2);
      RequireLeaf(root.children[0], 232, RanValueType::kElementReal);
      RequireLeaf(root.children[1], 233, RanValueType::kElementInteger);
      const slice_act_flexric_cell_dl_tx_power_t power = {
          root.children[0].real, root.children[1].integer};
      built = slice_act_build_flexric_style2_action104_cell_dl_tx_power(
          anchor, &power, &native, why, sizeof(why));
      break;
    }
    default:
      throw std::invalid_argument("native local-action path supports Actions 101..104");
  }
  if (!built) {
    throw std::invalid_argument(why);
  }
  return SubmitOwned(api_, native, scope_key, fencing_token, idempotency_key);
}

std::optional<PrbRatios> NativeFlexricRcTransport::Readback(
    const std::string& scope_key) const {
  return api_.Readback(scope_key);
}

}  // namespace oran_aic::slice_act
