from mpi4py import MPI
import openmdao.api as om

from mphys import MultipointParallelGroup
from mphys.mphys_vlm import VlmBuilderAeroOnly
from mphys.scenario_aero import ScenarioAero


class ParallelCruises(MultipointParallelGroup):
    def setup(self):
        # VLM options
        mesh_file = 'wing_VLM.dat'

        aero_builder = VlmBuilderAeroOnly(mesh_file)
        self.mphys_add_scenario('cruise',ScenarioAero(aero_builder=aero_builder,
                                                      in_MultipointParallelGroup=True))

        self.mphys_add_scenario('cruise_higher_aoa',ScenarioAero(aero_builder=aero_builder,
                                                                 in_MultipointParallelGroup=True))

class Top(om.Group):
    def setup(self):
        mach = 0.85,
        aoa0 = 0.0
        aoa1 = 2.0
        q_inf = 3000.
        vel = 178.
        mu = 3.5E-5

        dvs = self.add_subsystem('dvs', om.IndepVarComp(), promotes=['*'])
        dvs.add_output('aoa0', val=aoa0, units='deg')
        dvs.add_output('aoa1', val=aoa1, units='deg')
        dvs.add_output('mach', mach)
        dvs.add_output('q_inf', q_inf)
        dvs.add_output('vel', vel)
        dvs.add_output('mu', mu)

        self.add_subsystem('mp',ParallelCruises())
        for dv in ['mach', 'q_inf', 'vel', 'mu']:
            self.connect(dv, f'mp.cruise.{dv}')
        self.connect('aoa0','mp.cruise.aoa')
        for dv in ['mach', 'q_inf', 'vel', 'mu']:
            self.connect(dv, f'mp.cruise_higher_aoa.{dv}')
        self.connect('aoa1','mp.cruise_higher_aoa.aoa')
prob = om.Problem()
prob.model = Top()
prob.setup()

om.n2(prob, show_browser=False, outfile='mphys_vlm_2scenarios.html')

prob.run_model()
#if MPI.COMM_WORLD.rank == 0:
#    for scenario in ['cruise','cruise_higher_aoa']:
#        print('%s: C_L = %f, C_D=%f' % (scenario, prob.get_val(['mp.%s.C_L'%scenario],get_remote=True),
#                                                  prob.get_val(['mp.%s.C_D'%scenario],get_remote=True)))
if MPI.COMM_WORLD.rank == 0:
    scenario = 'cruise'
    print('%s: C_L = %f, C_D = %f' % (scenario, prob['mp.%s.C_L'%scenario],
                                                prob['mp.%s.C_D'%scenario]))
else:
    scenario = 'cruise_higher_aoa'
    print('%s: C_L = %f, C_D = %f' % (scenario, prob['mp.%s.C_L'%scenario],
                                                prob['mp.%s.C_D'%scenario]))
