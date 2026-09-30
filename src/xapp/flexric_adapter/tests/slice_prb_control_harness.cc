#include "rc_control_port.h"

#include <cassert>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>

using oran_aic::slice_act::EncodeStyle2Action6;
using oran_aic::slice_act::EncodeStyle2Action101;
using oran_aic::slice_act::EncodeStyle2Action102;
using oran_aic::slice_act::EncodeStyle2Action103;
using oran_aic::slice_act::EncodeStyle2Action104;
using oran_aic::slice_act::PlmnIdentity;
using oran_aic::slice_act::PrbRatios;
using oran_aic::slice_act::RcControlRequest;
using oran_aic::slice_act::RcTransport;
using oran_aic::slice_act::RanValueType;
using oran_aic::slice_act::SNssai;
using oran_aic::slice_act::SlicePrbRequest;
using oran_aic::slice_act::Style2Action6ControlPort;

namespace {

class MockTransport final : public RcTransport {
 public:
  bool Send(const std::string& scope_key, std::uint64_t,
            const std::string& idempotency_key,
            const SlicePrbRequest& source,
            const RcControlRequest& request) override {
    assert(!idempotency_key.empty());
    assert(request.header_format == 1);
    assert(request.style_type == 2);
    assert(request.action_id == 6);
    assert(request.message_format == 1);
    assert(request.ue_anchor_ref == source.ue_anchor_ref);
    assert(request.ran_parameters.size() == 1);
    assert(request.ran_parameters[0].id == 1);
    const auto& group = request.ran_parameters[0].children.at(0);
    assert(group.id == 2);
    assert(group.children.size() == 4);
    state_[scope_key] = {
        group.children[1].integer,
        group.children[2].integer,
        group.children[3].integer,
    };
    return true;
  }

  std::optional<PrbRatios> Readback(const std::string& scope_key) const override {
    const auto found = state_.find(scope_key);
    return found == state_.end() ? std::nullopt
                                : std::optional<PrbRatios>(found->second);
  }

 private:
  std::unordered_map<std::string, PrbRatios> state_;
};

SlicePrbRequest Request(PrbRatios ratios) {
  return {PlmnIdentity{"208", "95"}, SNssai{222, 0x00007B}, ratios,
          "imsi-208950000000032"};
}

}  // namespace

int main() {
  const auto encoded = EncodeStyle2Action6({Request({20, 80, 10})});
  const auto& member = encoded.ran_parameters[0].children[0]
                           .children[0].children[0].children[0];
  assert(member.children[0].octets ==
         std::vector<std::uint8_t>({0x02, 0xF8, 0x59}));
  assert(member.children[1].children[0].octets ==
         std::vector<std::uint8_t>({0xDE}));
  assert(member.children[1].children[1].octets ==
         std::vector<std::uint8_t>({0x00, 0x00, 0x7B}));
  std::cout << "normal:PASS\n";

  const std::string anchor = "imsi-208950000000032";
  const auto action101 = EncodeStyle2Action101({4, 19, anchor});
  assert(action101.action_id == 101 && action101.ran_parameters.size() == 1);
  const auto& mcs_root = action101.ran_parameters[0];
  assert(mcs_root.id == 201 && mcs_root.type == RanValueType::kStructure);
  assert(mcs_root.children.size() == 2);
  assert(mcs_root.children[0].id == 202 &&
         mcs_root.children[0].type == RanValueType::kElementInteger &&
         mcs_root.children[0].integer == 19);
  assert(mcs_root.children[1].id == 203 &&
         mcs_root.children[1].type == RanValueType::kElementInteger &&
         mcs_root.children[1].integer == 4);
  std::cout << "action101-logical-tree:PASS\n";

  const auto action102 = EncodeStyle2Action102({24, anchor});
  const auto& cap_root = action102.ran_parameters.at(0);
  assert(action102.action_id == 102 && cap_root.id == 211 &&
         cap_root.type == RanValueType::kStructure && cap_root.children.size() == 1);
  assert(cap_root.children[0].id == 212 &&
         cap_root.children[0].type == RanValueType::kElementInteger &&
         cap_root.children[0].integer == 24);
  std::cout << "action102-logical-tree:PASS\n";

  const auto action103 = EncodeStyle2Action103({1.25, anchor});
  const auto& priority_root = action103.ran_parameters.at(0);
  assert(action103.action_id == 103 && priority_root.id == 221 &&
         priority_root.type == RanValueType::kStructure &&
         priority_root.children.size() == 1);
  assert(priority_root.children[0].id == 222 &&
         priority_root.children[0].type == RanValueType::kElementReal &&
         priority_root.children[0].real == 1.25);
  std::cout << "action103-logical-tree-real:PASS\n";

  const auto action104 = EncodeStyle2Action104({3.75, 0x1234, anchor});
  const auto& power_root = action104.ran_parameters.at(0);
  assert(action104.action_id == 104 && power_root.id == 231 &&
         power_root.type == RanValueType::kStructure && power_root.children.size() == 2);
  assert(power_root.children[0].id == 232 &&
         power_root.children[0].type == RanValueType::kElementReal &&
         power_root.children[0].real == 3.75);
  assert(power_root.children[1].id == 233 &&
         power_root.children[1].type == RanValueType::kElementInteger &&
         power_root.children[1].integer == 0x1234);
  std::cout << "action104-logical-tree-real:PASS\n";

  try {
    EncodeStyle2Action6({SlicePrbRequest{PlmnIdentity{"208", "95"},
                                         std::nullopt, {20, 80, 10},
                                         "imsi-208950000000032"}});
    return 2;
  } catch (const std::invalid_argument&) {
    std::cout << "missing-snssai:PASS\n";
  }

  try {
    EncodeStyle2Action6({SlicePrbRequest{PlmnIdentity{"208", "95"},
                                         SNssai{0, std::nullopt}, {20, 80, 10},
                                         "imsi-208950000000032"}});
    return 3;
  } catch (const std::invalid_argument&) {
    std::cout << "invalid-sst:PASS\n";
  }

  MockTransport transport;
  Style2Action6ControlPort port(transport);
  const auto baseline = port.BootstrapBaseline("lab-setup", 1, Request({10, 70, 5}));
  assert(baseline.acknowledged && baseline.readback_verified && !baseline.previous.has_value());
  std::cout << "baseline-bootstrap:PASS\n";
  const auto changed = port.Apply("policy-1", 2, Request({20, 80, 10}));
  assert(changed.previous == std::optional<PrbRatios>({10, 70, 5}));
  const auto updated = port.Apply("policy-1", 3, Request({30, 90, 15}));
  assert(updated.previous == std::optional<PrbRatios>({20, 80, 10}));
  const auto restored = port.Rollback("policy-1", 4);
  assert(restored.readback_verified);
  assert(transport.Readback("208-95/222/00007B") ==
         std::optional<PrbRatios>({10, 70, 5}));
  std::cout << "rollback:PASS\n";
  return 0;
}
