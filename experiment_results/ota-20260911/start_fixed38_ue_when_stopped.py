#!/usr/bin/env python3
"""Start only a stopped UE using the inspected 38-PRB inputs; never restart it.

Initial placement uses one of the two declared cells, not a serving-cell
restriction or handover claim. No OAI/config files or TUNs are created or edited.
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import pwd
import subprocess
import sys


# 2026-09-24 23:23: all three on the HO re-sync CFO patch (oai_patches/nr_ue_ho_resync_rejects_implausible_cfo.w30.patch);
# ue1/ue3 7f275e08 (ue1 build, latest source), ue2 2bba3edf.  Previous: ue1 4076e5e3, ue2 4ab50ffc, ue3 beb823b4.
# 2026-09-27 19:5x: + agreeing fresh estimates replace the anchor (nr_ue_resync_offset_consensus_replaces_anchor.w30.patch);
# ue1/ue3 ea010d51, ue2 03548695.  Previous: ue1/ue3 3f124c7f, ue2 c6773846.
# 2026-09-27 19:0x: all three on the anchored re-sync offset (oai_patches/nr_ue_resync_offset_anchor_300hz.w30.patch);
# ue1/ue3 3f124c7f (ue1 build), ue2 c6773846.  Previous: ue1/ue3 6c2718ab, ue2 49e06cbe.
PROFILES = {
    'ue1': ('nr-ue.conf', 'd3c2c59ebca6985afbde0e82573f7e13b8c06b57548c5c257eef9c6c5a3afa60',
            'f89d7657dd5cf0a267fbd7b772c10ec7b4df1a41f1a736cd081d1f21f25ea3d2', '35D5F42', 3319680000),  # 2026-09-25 09:5x NCC wrap + first-NH-from-NAS-KgNB fix (nr_ue_ncc_wraps_modulo_8.w30.patch) on the hocfo build; was 2417b79d
             # 2026-09-24 20:25 ue1 back to the pre-09-24 build: today's build (532e90c4) missed DCIs (TBS-mismatch 1049/21 s, PUSCH DTX 29 %, 0.31 Mbps); this one 0 / 0.2 % / 5.21 Mbps
    'ue2': ('nr-ue.sst1.conf', 'f0ffb8e5100cac919a002c10c3ad738088d02a308ae3de61abdc57162fe69266',
            'ce040282a6839fdf35f9c38ceed738243a2dd63945d5fa1edcc2af8866deefb4', '352F0C1', 3349920000),  # 2026-09-28 20:3x USB swap test (was 35DA62D); 2026-09-25 09:5x NCC wrap fix; was c3a47dcf
             # 2026-09-24 22:16 ue2 back to its 12:55 build: the pre-09-24 build lacks the SSS/PSS sync fixes and failed to sync gnb1 (50+ in 4 min); the build was not the DCI cause
    'ue3': ('nr-ue.conf', 'e7ce4f8af3d41cd453494385dd199d3db0bcc2aa9c3e284668672a9e454e37e6',
            '351c5225c0b7cb5560f8fbeb516e09f99541562f61ea33e2fd578ff620f24ea9', '35DA62D', 3349920000),  # 2026-09-28 20:3x USB swap test (was 352F0C1); 2026-09-25 09:5x copy of the ue1 NCC wrap build; was 2417b79d
             # 2026-09-24 12:4x skip a stale RLC NACK (copy of the ue1 build)
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if (len(args) not in (1, 2) or args[0] not in PROFILES
            or (len(args) == 2 and args[1] not in ('gnb1', 'gnb2'))):
        raise SystemExit('usage: start_fixed38_ue_when_stopped.py ue1|ue2|ue3 [gnb1|gnb2]')
    tag = args[0]
    if os.geteuid() != 0 or os.environ.get('SUDO_USER') != 'lics-' + tag:
        raise SystemExit('Use the normal sudo lane on the selected UE host only')
    owner = pwd.getpwnam('lics-' + tag)
    home = Path(owner.pw_dir)
    build = home/'ai-ran-stage/oai-2026.w30/cmake_targets/ue-2026w30-uhd490/build'
    binary = build/'nr-uesoftmodem'
    profile_name, profile_sha, binary_sha, serial, carrier = PROFILES[tag]
    if len(args) == 2:
        carrier = {'gnb1': 3349920000, 'gnb2': 3319680000}[args[1]]
    profile = home/'ai-ran-stage/runtime/phase-b'/profile_name
    if digest(binary) != binary_sha or digest(profile) != profile_sha:
        raise SystemExit('START_REFUSED: inspected binary/profile hash changed')
    lock = os.open('/run/lock/aic-fixed38-ue-start.lock', os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        inventory = subprocess.run(['ps','-C','nr-uesoftmodem','-o','pid='],
                                   capture_output=True,text=True)
        if inventory.returncode not in (0,1) or inventory.stdout.strip():
            raise SystemExit('START_REFUSED: existing UE process or unreadable inventory; nothing stopped')
        env = os.environ.copy()
        env.update(LD_LIBRARY_PATH='/opt/uhd-4.9.0.0/lib:'+str(build),
                   UHD_IMAGES_DIR='/opt/uhd-4.9.0.0/share/uhd/images')
        probe = subprocess.run(['timeout','60','/opt/uhd-4.9.0.0/bin/uhd_usrp_probe',
                                '--args','type=b200,serial='+serial],env=env,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if probe.returncode:
            raise SystemExit('START_REFUSED: idle-device probe failed; no blind retry')
        # All three hosts use the value they were proven on. ue1 was briefly given a
        # deeper buffer because its overflow looked like the root cause; it is not --
        # the overflow is what happens after ue1 fails contention resolution at Msg4
        # and storms the RA procedure. Giving one host a different buffer only made
        # it incomparable to the two that work.
        frames = int(os.environ.get('AIC_UE_FRAMES') or 512)
        # 38 PRB at numerology 1 asks the radio for 15.36 MSps. When the master
        # clock is not an integer multiple of that, UHD resamples every buffer and
        # the cost shows up exactly as a short recv -- which is how ue1 dies.
        # 30.72e6 makes it a clean divide by two.
        # usrp_lib.cpp: set_tx_gain(gain_range.stop() - tx_gain), so --ue-txgain is
        # attenuation from maximum, not an absolute gain.  The default 0 means the UE
        # transmits at the B206mini's full 89.75 dB.  AIC_UE_TXGAIN backs that off.
        # 2026-09-24 04:4x: initial sync runs at the default rx gain 110 (-> 63 dB of 76) with AGC off,
        # and the SSS metric is an absolute amplitude against a fixed floor (sss_nr.h 30000): on the
        # marginal links every SSB search failed at SSS (ue1: 200/200) while the UE, once synced,
        # raised its own gain by 3-5 dB (adjust_rxgain).  Start 4 dB higher.  AIC_UE_RXGAIN overrides.
        # 06:27 measured on ue3 (SSB searches that passed SSS): gain 100 -> 0/2600, 110 -> 3/1400, 114 -> 58/19600,
        # 120 -> attached at once.  ue3's receive path is weaker than ue1/ue2 (fine at 110), so it gets its own gain.
        # 13:4x: set so each UE's home cell reads ~50 dB/RE at PBCH (AGC is off, so adjust_rxgain is never
        # applied): ue2 read rsrp 42 (+8) on gnb1 at 110 -> DL MCS 3-7, BLER 13-20 %, goodput pinned at 2.3 Mbps
        # on every board; ue1 48 (+2) on gnb2 at 110; ue3 51 (-1) at 120.
        # 2026-09-24 23:3x: ue1 112 -> 116 on gnb2.  Judged by DL DCI misses (gNB dlsch_rounds sum vs UE
        # 'DL harq' sum), not by adjust_rxgain: at 112 ue1 missed 6.6 % (ue3 at 120: 0.9 %) with the same
        # RSRP (-101/-100 dBm); at 116 (22:11-22:48) it missed 0.1 % over 1.23 M TBs.
        # 23:5x ue1 116 -> 120: at 116 the misses came back within minutes (0 % -> 3.3 %); ue3 decodes best
        # at 120 (61 dB/RE), so ue1 gets the same gain.
        # ue2 stays 118: 122 (23:43) could not sync gnb1 at all (2826 'synch Failed', PSS peak 79 dB).
        # Tried for ue2 118 -> 122 on gnb1: RSRP -109 dBm, 46 dB/RE, adjust_rxgain +4, 5.3 % DCI missed and 1443 CCE
        # failures in 30 s -- UE2 goodput (protected, 8.0) failed in all 576 judgements of a board.
        # 2026-09-29: gains belong to the radio, not the host -- after the USB swap ue3's PC drove ue2's old B206
        # at 113 (AGC 109) and missed 12k RARs.  Pick the gain by serial.
        # 05:2x correction: the maps below were calibrated per HOST position (path loss dominates);
        # keyed by serial, ue3's PC got ue2's 118 and read rsrp 59 dB/RE (adjust_rxgain -9) -> 5027
        # RAR failures.  Back to the host key.
        radio = tag
        rxgain = os.environ.get('AIC_UE_RXGAIN') or {'ue1': '116', 'ue2': '118', 'ue3': '111'}.get(radio, '110')
        if not re.fullmatch(r'[0-9]+(\.[0-9]+)?', rxgain) or not 90 <= float(rxgain) <= 123:
            raise SystemExit('START_REFUSED: AIC_UE_RXGAIN must be 90-123')
        # 13:0x: ue1 sees gnb1 15-20 dB weaker than its home gnb2 -- 110 cannot sync gnb1 after a steer,
        # 120-123 saturates gnb2 (rsrp 58-61, adjust_rxgain -8..-11).  The UE switches gain with the
        # carrier a handover moves it to (oai_patches/nr_ue_rxgain_by_carrier.w30.patch).
        by_carrier = (os.environ.get('AIC_UE_RXGAIN_BY_CARRIER')
                      or {'ue1': '3349920000:123,3319680000:116',   # gnb1 weak for ue1 (rsrp 46 at 120, +4); gnb2 48 at 110 (+2)
                          'ue2': '3349920000:118,3319680000:106',   # gnb1 42 at 110 (+8); gnb2 44 at 100 (+6)
                          # 09-25 10:47 ue3 steered to gnb1 at 120 saw rsrp 57 (adjust -7): the saturated PSS read the
                          # CFO as 7-9 kHz six times, it attached 8.8 kHz off and its RRCReconfigurationComplete was lost.
                          'ue3': '3349920000:111,3319680000:110',   # 09-29 05:2x 111: rsrp 59 at 118 (-9) with B206 35DA62D;   # gnb1 57 at 120 (-7); gnb2 60 at 120 (-10) on 09-25 12:1x (was 50 in the morning); start gain 113: ue3's home is gnb1 since placement B
                          }.get(radio, ''))
        if by_carrier and not re.fullmatch(r'[0-9]{10}:[0-9]{2,3}(,[0-9]{10}:[0-9]{2,3})*', by_carrier):
            raise SystemExit('START_REFUSED: AIC_UE_RXGAIN_BY_CARRIER must be <Hz>:<gain>[,...]')
        # 2026-09-26 23:4x: OAI's own --agc applies the UE's adjust_rxgain at every sync (and +3 dB per
        # failed sync).  Without it ue3, moved into gnb2 while a trial cut gnb2 by 9-15 dB, read rsrp
        # 37-39 dB/RE at its fixed carrier gain and asked +11..+13 dB that nothing applied -- thousands
        # of sync failures, a 165 s resync.  AGC_HOSTS names who runs it; AIC_UE_AGC=0/1 overrides.
        # 2026-09-29 23:5x ue1 off: after a failed hand-back resync the AGC raises +3 dB per failure to the
        # USRP max and has no way down -- boards 935/937/943 lost ue1 (287 s of 7134 failed syncs in 935).
        # ue1's carrier gains (gnb1 123 = already the USRP max, gnb2 116) leave the AGC nothing else to do.
        AGC_HOSTS = ('ue2',)   # 2026-09-29 03:3x ue3 off: its AGC re-set the gain >1200 times and RAR decoding failed (was ue1-3, 2026-09-27)
        agc = {'1': True, '0': False}.get(os.environ.get('AIC_UE_AGC', ''), tag in AGC_HOSTS)
        txgain = os.environ.get('AIC_UE_TXGAIN') or ''
        if txgain:
            if not re.fullmatch(r'[0-9]+(\.[0-9]+)?', txgain) or not 0 <= float(txgain) <= 40:
                raise SystemExit('START_REFUSED: AIC_UE_TXGAIN must be 0..40 dB of attenuation')
        mcr = os.environ.get('AIC_UE_MCR') or ''
        mcr = (',master_clock_rate=' + mcr) if mcr else ''
        # USB 자동절전을 **장치부터 루트 복합체까지** 고정한다.  2026-09-17 에 ue1 이
        # 다섯 시간 동안 시간당 3.6 회 죽었고 로그 26 개 중 19 개가
        # `uhd::usb_error` (LIBUSB_ERROR_IO · LIBUSB_TRANSFER_NO_DEVICE) 로 끝났다.
        # 장치 자신은 control=on 이었지만 **상위 허브가 auto + 지연 0ms** 라 즉시
        # 잠들 수 있었다 -- 허브가 자면 하위 전송이 깨진다.  손으로 넣은 설정은
        # 재부팅·재열거에 날아가므로 기동할 때마다 다시 건다.  읽기/쓰기 모두 실패해도
        # 기동은 계속한다(이 설정은 최적화이지 전제가 아니다).
        try:
            import glob as _glob
            for _d in _glob.glob('/sys/bus/usb/devices/*'):
                try:
                    if open(_d + '/idVendor').read().strip() != '2500':
                        continue
                except OSError:
                    continue
                _cur = os.path.realpath(_d)
                while _cur.startswith('/sys/devices') and _cur != '/sys/devices':
                    _f = os.path.join(_cur, 'power', 'control')
                    if os.path.exists(_f):
                        try:
                            with open(_f, 'w') as _h:
                                _h.write('on')
                        except OSError:
                            pass
                    _cur = os.path.dirname(_cur)
        except Exception:
            pass

        stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        log = home/('ota-fixed38-'+tag+'-'+stamp+'.log')
        fd = os.open(log, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        os.fchown(fd, owner.pw_uid, owner.pw_gid)
        command = ['chrt','-f','99',str(binary),'-O',str(profile),'-C',str(carrier),
                   '-r','38','--numerology','1','--band','78','--ssb','108',
                   *(() if os.environ.get('AIC_UE_NO_SCAN') else ('--ue-scan-carrier',)),
                   *(() if os.environ.get('AIC_UE_NO_FO') else ('--ue-fo-compensation',)),
                   '--ue-rxgain', rxgain,
                   *(('--agc',) if agc else ()),
                   *(('--ue-txgain', txgain) if txgain else ()),
                   '--usrp-args','type=b200,serial='+serial+',clock_source=internal,time_source=internal,num_recv_frames='+str(frames)+',num_send_frames='+str(frames)+mcr,
                   '--log_config.global_log_options','level,nocolor,time']
        try:
            if by_carrier:
                env = dict(env, AIC_UE_RXGAIN_BY_CARRIER=by_carrier)
            process = subprocess.Popen(command,cwd=build,env=env,stdin=subprocess.DEVNULL,
                                       stdout=fd,stderr=fd,start_new_session=True,close_fds=True)
        finally:
            os.close(fd)
        record = {'capturedAt':datetime.datetime.now().astimezone().isoformat(),
                  'host':tag,'pid':process.pid,'processExit':process.poll(),'log':str(log),
                  'binarySha256':binary_sha,'profile':str(profile),'profileSha256':profile_sha,
                  'carrierHz':carrier,'initialCell':'gnb1' if carrier==3349920000 else 'gnb2',
                  'initialAssociationIsLabSetup':True,'prb':38,'ssb':108,'rtPriority':99,
                  'cpuPinning':False,'agc':agc,'frames':frames,'configurationEdited':False,
                  'ueAttachmentVerified':False,'otaCompletionVerified':False}
        receipt = home/('ota-fixed38-'+tag+'-'+stamp+'.json')
        out = os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        os.fchown(out,owner.pw_uid,owner.pw_gid)
        with os.fdopen(out,'w') as stream:
            json.dump(record,stream,indent=2)
            stream.write('\n')
        print(json.dumps({**record,'receipt':str(receipt)}),flush=True)
    finally:
        os.close(lock)


if __name__ == '__main__':
    main()
