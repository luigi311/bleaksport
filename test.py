import asyncio
from bleaksport import ESSMux, discover_ess_devices, discover_running_devices

async def main():
    mac: str | None = None
    # Suppose your app provides a MAC (string) to connect to:
    devices = await discover_ess_devices()
    devices_running = await discover_running_devices()

    for d in devices:
        print(d.address, d.name)
        mac = d.address

    for d in devices_running:
        print(d.address, d.name)

    if not mac:
        print("No ESS devices found")
        exit()


    def on_status(msg: str) -> None:
        print(msg)

    async def on_sample(s):
        # s is ESSample: pressure_pa, altitude_m, temperature_c, humidity_pct
        print(f"p={s.pressure_pa:.0f} Pa  alt={s.altitude_m:.1f} m"
              f"{'' if s.temperature_c is None else f'  T={s.temperature_c:.2f} °C'}"
              f"{'' if s.humidity_pct is None else f'  RH={s.humidity_pct:.1f} %'}")

    mux = ESSMux(
        ess_addr=mac,
        poll_interval_s=1.0,    # only used if notify isn't supported
        on_sample=on_sample,
        on_status=on_status,
    )

    try:
        await mux.start()  # runs until disconnected; call mux.stop() from elsewhere to end
    finally:
        await mux.stop()

asyncio.run(main())
