#include "flexric_control_adapter.h"
#include "src/sm/rc_sm/ie/ir/ran_param_list.h"
#include "src/sm/rc_sm/ie/ir/ran_param_struct.h"

#include <cassert>
#include <iostream>
#include <string>
#include <unordered_map>
#include <unordered_set>

using oran_aic::slice_act::FlexricControlApi;
using oran_aic::slice_act::FlexricUeAnchorResolver;
using oran_aic::slice_act::NativeFlexricRcTransport;
using oran_aic::slice_act::PlmnIdentity;
using oran_aic::slice_act::PrbRatios;
using oran_aic::slice_act::SNssai;
using oran_aic::slice_act::SlicePrbRequest;
using oran_aic::slice_act::Style2Action6ControlPort;
using oran_aic::slice_act::EncodeStyle2Action101;
using oran_aic::slice_act::EncodeStyle2Action102;
using oran_aic::slice_act::EncodeStyle2Action103;
using oran_aic::slice_act::EncodeStyle2Action104;

extern "C" ue_id_e2sm_t cp_ue_id_e2sm(const ue_id_e2sm_t* source) {
  assert(source != nullptr);
  return *source;
}

extern "C" void free_rc_ctrl_req_data(rc_ctrl_req_data_t* request) {
  /* Production links FlexRIC's owning destructor. The short-lived harness
   * supplies this shim solely to exercise the complete builder/submit call. */
  (void)request;
}

namespace {

class AnchorResolver final : public FlexricUeAnchorResolver {
 public:
  AnchorResolver() { anchor_.type = GNB_UE_ID_E2SM; }

  const ue_id_e2sm_t* Resolve(const std::string& anchor_ref) const override {
    return anchor_ref == "imsi-208950000000032" ? &anchor_ : nullptr;
  }

 private:
  ue_id_e2sm_t anchor_{};
};

class MockFlexricApi final : public FlexricControlApi {
 public:
  bool WriteControl(const rc_ctrl_req_data_t& request,
                    const std::string& scope_key,
                    std::uint64_t fencing_token,
                    const std::string& idempotency_key) override {
    assert(scope_key == "208-95/222/00007B");
    assert(fencing_token > last_fencing_token_);
    assert(!idempotency_key.empty());
    assert(idempotency_keys_.insert(idempotency_key).second);
    last_fencing_token_ = fencing_token;
    assert(request.hdr.format == FORMAT_1_E2SM_RC_CTRL_HDR);
    assert(request.hdr.frmt_1.ric_style_type == 2);
    assert(request.msg.format == FORMAT_1_E2SM_RC_CTRL_MSG);
    assert(request.msg.frmt_1.sz_ran_param == 1);
    const seq_ran_param_t& root = request.msg.frmt_1.ran_param[0];
    switch (request.hdr.frmt_1.ctrl_act_id) {
      case 6: {
        assert(root.ran_param_id == 1);
        assert(root.ran_param_val.type == LIST_RAN_PARAMETER_VAL_TYPE);
        const ran_param_struct_t& group =
            root.ran_param_val.lst->lst_ran_param[0].ran_param_struct;
        assert(group.sz_ran_param_struct == 4);
        assert(group.ran_param_struct[0].ran_param_id == 3);
        assert(group.ran_param_struct[1].ran_param_id == 11);
        assert(group.ran_param_struct[2].ran_param_id == 12);
        assert(group.ran_param_struct[3].ran_param_id == 13);
        state_["208-95/222/00007B"] = PrbRatios{
            group.ran_param_struct[1].ran_param_val.flag_false->int_ran,
            group.ran_param_struct[2].ran_param_val.flag_false->int_ran,
            group.ran_param_struct[3].ran_param_val.flag_false->int_ran,
        };
        break;
      }
      case 101:
        AssertStructure(root, 201, 2);
        AssertInteger(root.ran_param_val.strct->ran_param_struct[0], 202, 19);
        AssertInteger(root.ran_param_val.strct->ran_param_struct[1], 203, 4);
        break;
      case 102:
        AssertStructure(root, 211, 1);
        AssertInteger(root.ran_param_val.strct->ran_param_struct[0], 212, 24);
        break;
      case 103:
        AssertStructure(root, 221, 1);
        AssertReal(root.ran_param_val.strct->ran_param_struct[0], 222, 1.25);
        break;
      case 104:
        AssertStructure(root, 231, 2);
        AssertReal(root.ran_param_val.strct->ran_param_struct[0], 232, 3.75);
        AssertInteger(root.ran_param_val.strct->ran_param_struct[1], 233, 0x1234);
        break;
      default:
        assert(false && "unexpected Action ID");
    }
    ++writes_;
    return true;
  }

  std::optional<PrbRatios> Readback(const std::string& scope_key) const override {
    const auto found = state_.find(scope_key);
    return found == state_.end() ? std::nullopt
                                : std::optional<PrbRatios>(found->second);
  }

  int writes() const { return writes_; }

 private:
  static void AssertStructure(const seq_ran_param_t& root,
                              uint32_t id,
                              size_t children) {
    assert(root.ran_param_id == id);
    assert(root.ran_param_val.type == STRUCTURE_RAN_PARAMETER_VAL_TYPE);
    assert(root.ran_param_val.strct != nullptr);
    assert(root.ran_param_val.strct->sz_ran_param_struct == children);
  }

  static void AssertInteger(const seq_ran_param_t& leaf,
                            uint32_t id,
                            int64_t value) {
    assert(leaf.ran_param_id == id);
    assert(leaf.ran_param_val.type == ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE);
    assert(leaf.ran_param_val.flag_false != nullptr);
    assert(leaf.ran_param_val.flag_false->type == INTEGER_RAN_PARAMETER_VALUE);
    assert(leaf.ran_param_val.flag_false->int_ran == value);
  }

  static void AssertReal(const seq_ran_param_t& leaf,
                         uint32_t id,
                         double value) {
    assert(leaf.ran_param_id == id);
    assert(leaf.ran_param_val.type == ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE);
    assert(leaf.ran_param_val.flag_false != nullptr);
    assert(leaf.ran_param_val.flag_false->type == REAL_RAN_PARAMETER_VALUE);
    assert(leaf.ran_param_val.flag_false->real_ran == value);
  }

  std::unordered_map<std::string, PrbRatios> state_;
  std::unordered_set<std::string> idempotency_keys_;
  std::uint64_t last_fencing_token_{};
  int writes_{};
};

SlicePrbRequest Request(PrbRatios ratios) {
  return {PlmnIdentity{"208", "95"}, SNssai{222, 0x00007B}, ratios,
          "imsi-208950000000032"};
}

}  // namespace

int main() {
  AnchorResolver resolver;
  MockFlexricApi api;
  NativeFlexricRcTransport transport(resolver, api);
  Style2Action6ControlPort port(transport);
  const auto baseline = port.BootstrapBaseline("lab-setup", 1, Request({10, 70, 5}));
  assert(baseline.acknowledged && baseline.readback_verified);
  const auto applied = port.Apply("policy-1", 2, Request({20, 80, 10}));
  assert(applied.acknowledged && applied.readback_verified);
  const auto updated = port.Apply("policy-1", 3, Request({30, 90, 15}));
  assert(updated.acknowledged && updated.readback_verified);
  const auto rolled_back = port.Rollback("policy-1", 4);
  assert(rolled_back.acknowledged && rolled_back.readback_verified);
  assert(api.Readback("208-95/222/00007B") ==
         std::optional<PrbRatios>({10, 70, 5}));
  assert(api.writes() == 4);

  const std::string scope = "208-95/222/00007B";
  const std::string anchor = "imsi-208950000000032";
  assert(transport.WriteControl(scope, 5, "action101",
                                EncodeStyle2Action101({4, 19, anchor})));
  assert(transport.WriteControl(scope, 6, "action102",
                                EncodeStyle2Action102({24, anchor})));
  assert(transport.WriteControl(scope, 7, "action103",
                                EncodeStyle2Action103({1.25, anchor})));
  assert(transport.WriteControl(scope, 8, "action104",
                                EncodeStyle2Action104({3.75, 0x1234, anchor})));
  assert(api.writes() == 8);
  std::cout << "native-chain:PASS\n";
  return 0;
}
