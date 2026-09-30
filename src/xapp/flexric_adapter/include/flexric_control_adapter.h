#pragma once

#include "flexric_style2_action6_encoder.h"
#include "rc_control_port.h"

#include <string>

namespace oran_aic::slice_act {

class FlexricUeAnchorResolver {
 public:
  virtual ~FlexricUeAnchorResolver() = default;
  virtual const ue_id_e2sm_t* Resolve(const std::string& anchor_ref) const = 0;
};

class FlexricControlApi {
 public:
  virtual ~FlexricControlApi() = default;
  virtual bool WriteControl(const rc_ctrl_req_data_t& request,
                            const std::string& scope_key,
                            std::uint64_t fencing_token,
                            const std::string& idempotency_key) = 0;
  virtual std::optional<PrbRatios> Readback(const std::string& scope_key) const = 0;
};

/* Concrete project-side RcTransport: resolve Header Format 1's UE anchor,
 * build the owning FlexRIC IR, submit it, and delegate typed readback. */
class NativeFlexricRcTransport final : public RcTransport {
 public:
  NativeFlexricRcTransport(const FlexricUeAnchorResolver& resolver,
                           FlexricControlApi& api)
      : resolver_(resolver), api_(api) {}

  bool Send(const std::string& scope_key, std::uint64_t fencing_token,
            const std::string& idempotency_key,
            const SlicePrbRequest& source,
            const RcControlRequest& request) override;
  // Submit an already-validated logical request for deployment-local Action
  // 101--104 through the same owning FlexRIC WriteControl boundary.
  bool WriteControl(const std::string& scope_key,
                    std::uint64_t fencing_token,
                    const std::string& idempotency_key,
                    const RcControlRequest& request);
  std::optional<PrbRatios> Readback(const std::string& scope_key) const override;

 private:
  const FlexricUeAnchorResolver& resolver_;
  FlexricControlApi& api_;
};

}  // namespace oran_aic::slice_act
