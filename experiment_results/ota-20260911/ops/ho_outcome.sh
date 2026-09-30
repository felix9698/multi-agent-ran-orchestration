# per HO-in: did it complete, integrity failures in the next 3000 lines
L=$1
grep -a -n "Handover triggered for UE" "$L" | while IFS=: read n rest; do
  ue=$(echo "$rest" | grep -o "for UE [0-9]*" | cut -d' ' -f3)
  w=$(sed -n "$n,$((n+3000))p" "$L")
  c=$(echo "$w" | grep -a -c "handover for UE $ue/.*complete!")
  i=$(echo "$w" | grep -a -c "integrity failed")
  o=$(echo "$w" | grep -a -c "Ongoing handover for UE $ue,")
  echo "$(echo "$rest" | cut -c1-12) ue=$ue complete=$c integrityFail=$i ongoingRefusals=$o"
done
# (moved into ops 2026-09-25; run_blocks_campaign.sh sums these per board, plan V47 section 4)
