def altitude_from_pressure(pressure_pa, sea_level_pa=101325):
    return 44330 * (1 - (pressure_pa / sea_level_pa) ** (1 / 5.255))
