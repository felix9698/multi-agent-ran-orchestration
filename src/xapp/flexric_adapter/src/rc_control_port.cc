#include "rc_control_port.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace oran_aic::slice_act {
namespace {

RanParameter Integer(std::int64_t id, std::string name, std::int64_t value) {
  RanParameter result;
  result.id = id;
  result.name = std::move(name);
  result.type = RanValueType::kElementInteger;
  result.integer = value;
  return result;
}

RanParameter Real(std::int64_t id, std::string name, double value) {
  RanParameter result;
  result.id = id;
  result.name = std::move(name);
  result.type = RanValueType::kElementReal;
  result.real = value;
  return result;
}

RanParameter Octets(std::int64_t id, std::string name,
                    std::vector<std::uint8_t> value) {
  RanParameter result;
  result.id = id;
  result.name = std::move(name);
  result.type = RanValueType::kElementOctets;
  result.octets = std::move(value);
  return result;
}

RanParameter Container(std::int64_t id, std::string name, RanValueType type,
                       std::vector<RanParameter> children) {
  RanParameter result;
  result.id = id;
  result.name = std::move(name);
  result.type = type;
  result.children = std::move(children);
  return result;
}

std::vector<std::uint8_t> EncodePlmn(const PlmnIdentity& plmn) {
  if (plmn.mcc.size() != 3 || (plmn.mnc.size() != 2 && plmn.mnc.size() != 3) ||
      !std::all_of(plmn.mcc.begin(), plmn.mcc.end(),
                   [](unsigned char value) { return std::isdigit(value) != 0; }) ||
      !std::all_of(plmn.mnc.begin(), plmn.mnc.end(),
                   [](unsigned char value) { return std::isdigit(value) != 0; })) {
    throw std::invalid_argument("PLMN identity requires a 3-digit MCC and 2/3-digit MNC");
  }
  const auto digit = [](char value) { return static_cast<std::uint8_t>(value - '0'); };
  const std::uint8_t mnc3 = plmn.mnc.size() == 3 ? digit(plmn.mnc[2]) : 0xF;
  return {
      static_cast<std::uint8_t>(digit(plmn.mcc[1]) << 4 | digit(plmn.mcc[0])),
      static_cast<std::uint8_t>(mnc3 << 4 | digit(plmn.mcc[2])),
      static_cast<std::uint8_t>(digit(plmn.mnc[1]) << 4 | digit(plmn.mnc[0])),
  };
}

void ValidateRatios(const PrbRatios& ratios) {
  const bool bounded = 0 <= ratios.minimum && ratios.minimum <= 100 &&
                       0 <= ratios.maximum && ratios.maximum <= 100 &&
                       0 <= ratios.dedicated && ratios.dedicated <= 100;
  if (!bounded) {
    throw std::invalid_argument("PRB policy ratios must be integers in 0..100");
  }
  if (!(ratios.dedicated <= ratios.minimum && ratios.minimum <= ratios.maximum)) {
    throw std::invalid_argument("ratios require dedicated <= minimum <= maximum");
  }
}

std::string ScopeKey(const SlicePrbRequest& request) {
  if (!request.snssai.has_value()) {
    throw std::invalid_argument("S-NSSAI is mandatory for Style 2 Action 6");
  }
  if (request.ue_anchor_ref.empty()) {
    throw std::invalid_argument("Control Header Format 1 requires a resolved UE anchor");
  }
  const auto& snssai = *request.snssai;
  if (snssai.sst == 0 || (snssai.sd.has_value() && *snssai.sd > 0xFFFFFF)) {
    throw std::invalid_argument("S-NSSAI SST/SD is outside its encoded range");
  }
  std::ostringstream key;
  key << request.plmn.mcc << '-' << request.plmn.mnc << '/'
      << static_cast<unsigned>(snssai.sst) << '/';
  if (snssai.sd.has_value()) {
    key << std::uppercase << std::hex << std::setfill('0') << std::setw(6)
        << *snssai.sd;
  } else {
    key << '-';
  }
  return key.str();
}

SlicePrbRequest WithRatios(const SlicePrbRequest& request, PrbRatios ratios) {
  SlicePrbRequest result = request;
  result.ratios = ratios;
  return result;
}

}  // namespace

bool PrbRatios::operator==(const PrbRatios& other) const {
  return minimum == other.minimum && maximum == other.maximum &&
         dedicated == other.dedicated;
}

RcControlRequest EncodeStyle2Action6(const std::vector<SlicePrbRequest>& groups) {
  if (groups.empty()) {
    throw std::invalid_argument("RRM Policy Ratio List must not be empty");
  }
  std::int64_t minimum_sum = 0;
  std::int64_t dedicated_sum = 0;
  std::set<std::string> scopes;
  std::vector<RanParameter> encoded_groups;
  encoded_groups.reserve(groups.size());

  for (const auto& request : groups) {
    const std::string scope_key = ScopeKey(request);
    if (!scopes.insert(scope_key).second) {
      throw std::invalid_argument("duplicate S-NSSAI in RRM Policy Ratio List");
    }
    ValidateRatios(request.ratios);
    minimum_sum += request.ratios.minimum;
    dedicated_sum += request.ratios.dedicated;

    const SNssai& snssai = *request.snssai;
    std::vector<RanParameter> snssai_children;
    snssai_children.push_back(Octets(9, "SST", {snssai.sst}));
    if (snssai.sd.has_value()) {
      const auto sd = *snssai.sd;
      if (sd > 0xFFFFFF) {
        throw std::invalid_argument("S-NSSAI SD must fit in 24 bits");
      }
      snssai_children.push_back(Octets(
          10, "SD", {static_cast<std::uint8_t>(sd >> 16),
                     static_cast<std::uint8_t>(sd >> 8),
                     static_cast<std::uint8_t>(sd)}));
    }
    RanParameter member = Container(
        6, "RRM Policy Member", RanValueType::kStructure,
        {Octets(7, "PLMN Identity", EncodePlmn(request.plmn)),
         Container(8, "S-NSSAI", RanValueType::kStructure,
                   std::move(snssai_children))});
    RanParameter policy = Container(
        3, "RRM Policy", RanValueType::kStructure,
        {Container(5, "RRM Policy Member List", RanValueType::kList,
                   {std::move(member)})});
    encoded_groups.push_back(Container(
        2, "RRM Policy Ratio Group", RanValueType::kStructure,
        {std::move(policy),
         Integer(11, "Min PRB Policy Ratio", request.ratios.minimum),
         Integer(12, "Max PRB Policy Ratio", request.ratios.maximum),
         Integer(13, "Dedicated PRB Policy Ratio", request.ratios.dedicated)}));
  }
  if (dedicated_sum > 100 || minimum_sum > 100) {
    throw std::invalid_argument("aggregate dedicated/minimum ratios must not exceed 100");
  }
  RcControlRequest result;
  result.ue_anchor_ref = groups.front().ue_anchor_ref;
  if (!std::all_of(groups.begin(), groups.end(), [&](const SlicePrbRequest& item) {
        return item.ue_anchor_ref == result.ue_anchor_ref;
      })) {
    throw std::invalid_argument("one Control Request cannot mix UE anchors");
  }
  result.ran_parameters.push_back(Container(
      1, "RRM Policy Ratio List", RanValueType::kList,
      std::move(encoded_groups)));
  return result;
}

RcControlRequest EncodeStyle2Action101(const DlMcsBoundsRequest& request) {
  if (request.ue_anchor_ref.empty()) {
    throw std::invalid_argument("Control Header Format 1 requires a resolved UE anchor");
  }
  if (request.minimum < 0 || request.maximum > 28 ||
      request.minimum > request.maximum) {
    throw std::invalid_argument("DL MCS bounds require 0 <= minimum <= maximum <= 28");
  }
  RcControlRequest result;
  result.action_id = 101;
  result.ue_anchor_ref = request.ue_anchor_ref;
  result.ran_parameters.push_back(Container(
      201, "DL MCS Bounds", RanValueType::kStructure,
      {Integer(202, "DL MCS Maximum", request.maximum),
       Integer(203, "DL MCS Minimum", request.minimum)}));
  return result;
}

RcControlRequest EncodeStyle2Action102(const UeDlPrbCapRequest& request) {
  if (request.ue_anchor_ref.empty()) {
    throw std::invalid_argument("Control Header Format 1 requires a resolved UE anchor");
  }
  if (request.maximum < 0 || request.maximum > 275) {
    throw std::invalid_argument("Maximum DL PRBs must be an integer in 0..275");
  }
  RcControlRequest result;
  result.action_id = 102;
  result.ue_anchor_ref = request.ue_anchor_ref;
  result.ran_parameters.push_back(Container(
      211, "UE DL PRB Cap", RanValueType::kStructure,
      {Integer(212, "Maximum DL PRBs", request.maximum)}));
  return result;
}

RcControlRequest EncodeStyle2Action103(const UePfWeightRequest& request) {
  if (request.ue_anchor_ref.empty()) {
    throw std::invalid_argument("Control Header Format 1 requires a resolved UE anchor");
  }
  if (!std::isfinite(request.pf_weight) || request.pf_weight < 0.001 ||
      request.pf_weight > 100.0) {
    throw std::invalid_argument("PF Weight must be a finite REAL in 0.001..100");
  }
  RcControlRequest result;
  result.action_id = 103;
  result.ue_anchor_ref = request.ue_anchor_ref;
  result.ran_parameters.push_back(Container(
      221, "UE PF Weight", RanValueType::kStructure,
      {Real(222, "PF Weight", request.pf_weight)}));
  return result;
}

RcControlRequest EncodeStyle2Action104(const CellDlTxPowerRequest& request) {
  if (request.ue_anchor_ref.empty()) {
    throw std::invalid_argument("Control Header Format 1 requires a resolved UE anchor");
  }
  if (!std::isfinite(request.tx_attenuation_db)) {
    throw std::invalid_argument("DL TX Attenuation dB must be a finite REAL");
  }
  if (request.target_gnb_id < 0 ||
      static_cast<std::uint64_t>(request.target_gnb_id) >
          std::numeric_limits<std::uint32_t>::max()) {
    throw std::invalid_argument("Target gNB ID must fit uint32");
  }
  RcControlRequest result;
  result.action_id = 104;
  result.ue_anchor_ref = request.ue_anchor_ref;
  result.ran_parameters.push_back(Container(
      231, "Cell DL TX Power Control", RanValueType::kStructure,
      {Real(232, "DL TX Attenuation dB", request.tx_attenuation_db),
       Integer(233, "Target gNB ID", request.target_gnb_id)}));
  return result;
}

DispatchResult Style2Action6ControlPort::Apply(const std::string& policy_id,
                                                std::uint64_t fencing_token,
                                                const SlicePrbRequest& request) {
  const std::string scope_key = ScopeKey(request);
  if (fencing_token <= last_token_[scope_key]) {
    throw std::invalid_argument("stale fencing token");
  }
  RcControlRequest encoded = EncodeStyle2Action6({request});
  const std::optional<PrbRatios> previous = transport_.Readback(scope_key);
  if (!previous.has_value()) {
    throw std::invalid_argument("slice quota write requires a restorable previous readback");
  }
  if (!(*previous == request.ratios) && previous_.find(policy_id) == previous_.end()) {
    previous_[policy_id] = PreviousValue{scope_key, WithRatios(request, *previous)};
  }
  last_token_[scope_key] = fencing_token;
  const bool acknowledged = transport_.Send(
      scope_key, fencing_token, "apply:" + policy_id + ":" + std::to_string(fencing_token),
      request, encoded);
  const bool readback_verified = acknowledged &&
      transport_.Readback(scope_key) == std::optional<PrbRatios>(request.ratios);
  return {acknowledged, readback_verified, previous,
          readback_verified ? "quota applied and read back" :
                              "ACK is not effect evidence"};
}

DispatchResult Style2Action6ControlPort::BootstrapBaseline(
    const std::string& setup_id, std::uint64_t fencing_token,
    const SlicePrbRequest& request) {
  const std::string scope_key = ScopeKey(request);
  if (fencing_token <= last_token_[scope_key]) {
    throw std::invalid_argument("stale baseline-bootstrap fencing token");
  }
  if (transport_.Readback(scope_key).has_value()) {
    throw std::invalid_argument("baseline bootstrap requires an empty quota table");
  }
  const RcControlRequest encoded = EncodeStyle2Action6({request});
  last_token_[scope_key] = fencing_token;
  const bool acknowledged = transport_.Send(
      scope_key, fencing_token,
      "baseline-bootstrap:" + setup_id + ":" + std::to_string(fencing_token),
      request, encoded);
  const bool verified = acknowledged &&
      transport_.Readback(scope_key) == std::optional<PrbRatios>(request.ratios);
  return {acknowledged, verified, std::nullopt,
          verified ? "setup-only baseline bootstrap applied and read back" :
                     "baseline bootstrap not verified"};
}

DispatchResult Style2Action6ControlPort::Rollback(const std::string& policy_id,
                                                   std::uint64_t fencing_token) {
  const auto found = previous_.find(policy_id);
  if (found == previous_.end()) {
    throw std::invalid_argument("no restorable previous value");
  }
  const PreviousValue prior = found->second;
  if (fencing_token <= last_token_[prior.scope_key]) {
    throw std::invalid_argument("stale rollback fencing token");
  }
  const RcControlRequest encoded = EncodeStyle2Action6({prior.request});
  const std::optional<PrbRatios> current = transport_.Readback(prior.scope_key);
  last_token_[prior.scope_key] = fencing_token;
  const bool acknowledged = transport_.Send(
      prior.scope_key, fencing_token,
      "rollback:" + policy_id + ":" + std::to_string(fencing_token),
      prior.request, encoded);
  const bool restored = acknowledged &&
      transport_.Readback(prior.scope_key) == std::optional<PrbRatios>(prior.request.ratios);
  if (restored) {
    previous_.erase(found);
  }
  return {acknowledged, restored, current,
          restored ? "previous quota restored and read back" :
                     "rollback not verified"};
}

}  // namespace oran_aic::slice_act
