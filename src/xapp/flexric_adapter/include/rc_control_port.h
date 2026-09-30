#pragma once

// Forked from the released bundle's fail-closed port.  The bundle remains
// immutable; this copy implements E2SM-RC v1.03 Style 2 / Action 6 and the
// deployment-local Actions 101--104 advertised by the project gNB patch.

#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace oran_aic::slice_act {

struct PlmnIdentity {
  std::string mcc;
  std::string mnc;
};

struct SNssai {
  std::uint8_t sst{};
  std::optional<std::uint32_t> sd;
};

struct PrbRatios {
  std::int64_t minimum{};
  std::int64_t maximum{};
  std::int64_t dedicated{};

  bool operator==(const PrbRatios& other) const;
};

struct SlicePrbRequest {
  PlmnIdentity plmn;
  std::optional<SNssai> snssai;
  PrbRatios ratios;
  // Resolved by the near-RT slice/UE inventory before Header Format 1 build.
  std::string ue_anchor_ref;
};

struct DlMcsBoundsRequest {
  std::int64_t minimum{};
  std::int64_t maximum{};
  std::string ue_anchor_ref;
};

struct UeDlPrbCapRequest {
  std::int64_t maximum{};
  std::string ue_anchor_ref;
};

struct UePfWeightRequest {
  double pf_weight{};
  std::string ue_anchor_ref;
};

struct CellDlTxPowerRequest {
  double tx_attenuation_db{};
  std::int64_t target_gnb_id{};
  std::string ue_anchor_ref;
};

enum class RanValueType {
  kElementInteger,
  kElementReal,
  kElementOctets,
  kList,
  kStructure,
};

struct RanParameter {
  std::int64_t id{};
  std::string name;
  RanValueType type{RanValueType::kStructure};
  std::int64_t integer{};
  double real{};
  std::vector<std::uint8_t> octets;
  std::vector<RanParameter> children;
};

struct RcControlRequest {
  std::int64_t header_format{1};
  std::int64_t style_type{2};
  std::int64_t action_id{6};
  std::int64_t message_format{1};
  std::string ue_anchor_ref;
  std::vector<RanParameter> ran_parameters;
};

struct DispatchResult {
  bool acknowledged{};
  bool readback_verified{};
  std::optional<PrbRatios> previous;
  std::string detail;
};

class RcTransport {
 public:
  virtual ~RcTransport() = default;
  virtual bool Send(const std::string& scope_key, std::uint64_t fencing_token,
                    const std::string& idempotency_key,
                    const SlicePrbRequest& source,
                    const RcControlRequest& request) = 0;
  virtual std::optional<PrbRatios> Readback(const std::string& scope_key) const = 0;
};

// Throws std::invalid_argument before allocation/transport on malformed input.
RcControlRequest EncodeStyle2Action6(const std::vector<SlicePrbRequest>& groups);
RcControlRequest EncodeStyle2Action101(const DlMcsBoundsRequest& request);
RcControlRequest EncodeStyle2Action102(const UeDlPrbCapRequest& request);
RcControlRequest EncodeStyle2Action103(const UePfWeightRequest& request);
RcControlRequest EncodeStyle2Action104(const CellDlTxPowerRequest& request);

class Style2Action6ControlPort {
 public:
  explicit Style2Action6ControlPort(RcTransport& transport) : transport_(transport) {}

  // Setup-only path for an empty OAI quota table. It establishes the first
  // explicit readback baseline but is not a policy-enforcement success.
  DispatchResult BootstrapBaseline(const std::string& setup_id,
                                   std::uint64_t fencing_token,
                                   const SlicePrbRequest& request);
  DispatchResult Apply(const std::string& policy_id, std::uint64_t fencing_token,
                       const SlicePrbRequest& request);
  DispatchResult Rollback(const std::string& policy_id, std::uint64_t fencing_token);

 private:
  struct PreviousValue {
    std::string scope_key;
    SlicePrbRequest request;
  };

  RcTransport& transport_;
  std::unordered_map<std::string, PreviousValue> previous_;
  std::unordered_map<std::string, std::uint64_t> last_token_;
};

}  // namespace oran_aic::slice_act
