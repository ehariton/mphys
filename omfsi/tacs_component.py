from __future__ import division, print_function
import numpy as np

from openmdao.api import ImplicitComponent, ExplicitComponent
from tacs import TACS,functions

class TacsMesh(ExplicitComponent):
    """
    Component to read the initial mesh coordinates with TACS

    Assumptions:
        - User will provide a tacs_setup function that assigns tacs elements to the mesh
          => tacs = tacs_mesh_setup(comm)

    """
    def initialize(self):
        self.options.declare('tacs_mesh_setup', default = None, desc='Function to setup tacs')
        self.options['distributed'] = True

    def setup(self):

        # TACS assembler setup
        tacs_setup = self.options['tacs_mesh_setup']
        tacs = tacs_setup(self.comm)

        self.tacs = tacs

        # create some TACS bvecs that will be needed later
        self.xpts  = tacs.createNodeVec()
        self.tacs.getNodes(self.xpts)

        # OpenMDAO setup
        node_size  =     self.xpts.getArray().size
        self.add_output('x_s0', shape=node_size, desc='structural node coordinates')

    def compute(self,inputs,outputs):
        outputs['x_s0'] = self.xpts.getArray()

class TacsSolver(ImplicitComponent):
    """
    Component to perform TACS steady analysis

    Assumptions:
        - User will provide a tacs_solver_setup function that gives some pieces
          required for the tacs solver
          => tacs, mat, pc, gmres, struct_ndv = tacs_solver_setup(comm)
        - The TACS steady residual is R = K * u_s - f_s = 0

    """
    def initialize(self):

        self.options.declare('tacs_solver_setup', default = None, desc='Function to setup tacs')
        self.options['distributed'] = True

        self.tacs = None
        self.pc = None


        self.res = None
        self.ans = None
        self.struct_rhs = None
        self.psi_s = None
        self.x_save = None

        self.transposed = False

        #TODO more generic handling of writing f5 files
        self.write_f5 = True
        self.check_partials = True

    def setup(self):
#        self.set_check_partial_options(wrt='*',directional=True)

        # TACS assembler setup
        tacs_solver_setup = self.options['tacs_solver_setup']
        tacs, mat, pc, gmres, ndv = tacs_solver_setup(self.comm)

        self.tacs = tacs
        self.pc = pc
        self.gmres = gmres
        self.mat = mat

        # create some TACS bvecs that will be needed later
        self.res        = tacs.createVec()
        self.force      = tacs.createVec()
        self.ans        = tacs.createVec()
        self.struct_rhs = tacs.createVec()
        self.psi_s      = tacs.createVec()
        self.xpt_sens   = tacs.createNodeVec()

        # OpenMDAO setup

        state_size = self.ans.getArray().size
        node_size  = self.xpt_sens.getArray().size
        self.ndof = int(state_size/(node_size/3))

        s_list = self.comm.allgather(state_size)
        n_list = self.comm.allgather(node_size)
        irank  = self.comm.rank

        s1 = np.sum(s_list[:irank])
        s2 = np.sum(s_list[:irank+1])
        n1 = np.sum(n_list[:irank])
        n2 = np.sum(n_list[:irank+1])


        # inputs
        self.add_input('dv_struct', shape=ndv                                                 , desc='tacs design variables')
        self.add_input('x_s0',      shape=node_size , src_indices=np.arange(n1, n2, dtype=int), desc='structural node coordinates')
        self.add_input('f_s',       shape=state_size, src_indices=np.arange(s1, s2, dtype=int), desc='structural load vector')

        # outputs
        self.add_output('u_s',      shape=state_size, val = np.zeros(state_size),desc='structural state vector')

        # partials
        #self.declare_partials('u_s',['dv_struct','x_s0','f_s'])

    def _update_internal(self,inputs,outputs=None):
        self.tacs.setDesignVars(np.array(inputs['dv_struct'],dtype=TACS.dtype))

        xpts = self.tacs.createNodeVec()
        self.tacs.getNodes(xpts)
        xpts_array = xpts.getArray()
        xpts_array[:] = inputs['x_s0']
        self.tacs.setNodes(xpts)

        pc     = self.pc
        alpha = 1.0
        beta  = 0.0
        gamma = 0.0

        xpts = self.tacs.createNodeVec()
        self.tacs.getNodes(xpts)
        xpts_array = xpts.getArray()
        xpts_array[:] = inputs['x_s0']
        self.tacs.setNodes(xpts)

        res = self.tacs.createVec()
        res_array = res.getArray()
        res_array[:] = 0.0

        self.tacs.assembleJacobian(alpha,beta,gamma,res,self.mat)
        pc.factor()

        if outputs is not None:
            ans = self.ans
            ans_array = ans.getArray()
            ans_array[:] = outputs['u_s']
            self.tacs.applyBCs(ans)

            self.tacs.setVariables(ans)

    def apply_nonlinear(self, inputs, outputs, residuals):
        tacs = self.tacs
        res  = self.res
        ans  = self.ans

        self._update_internal(inputs,outputs)

        res_array = res.getArray()
        res_array[:] = 0.0

        # K * u
        tacs.assembleRes(res)

        # Add the external loads
        res_array[:] -= inputs['f_s']

        # Apply BCs to the residual
        tacs.applyBCs(res)

        residuals['u_s'][:] = res_array[:]

    def solve_nonlinear(self, inputs, outputs):
        tacs   = self.tacs
        force  = self.force
        ans    = self.ans
        pc     = self.pc
        gmres  = self.gmres

        self._update_internal(inputs)
        # solve the linear system
        force_array = force.getArray()
        force_array[:] = inputs['f_s']
        tacs.applyBCs(force)

        gmres.solve(force, ans)
        ans_array = ans.getArray()
        outputs['u_s'] = ans_array[:]
        tacs.setVariables(ans)

        if self.write_f5:
            flag = (TACS.ToFH5.NODES |
                    TACS.ToFH5.DISPLACEMENTS |
                    TACS.ToFH5.STRAINS |
                    TACS.ToFH5.EXTRAS)
            f5 = TACS.ToFH5(tacs, TACS.PY_SHELL, flag)
            f5.writeToFile('wing.f5')

    def solve_linear(self,d_outputs,d_residuals,mode):
        if mode == 'fwd':
            if self.check_partials:
                print ('solver fwd')
            else:
                raise ValueError('forward mode requested but not implemented')

        if mode == 'rev':
            tacs = self.tacs
            gmres = self.gmres

            # if nonsymmetric, we need to form the transpose Jacobian
            #if self._design_vector_changed(inputs['dv_struct']) or not self.transposed:
            #    alpha = 1.0
            #    beta  = 0.0
            #    gamma = 0.0

            #    tacs.assembleJacobian(alpha,beta,gamma,res,self.mat,matOr=TACS.PY_TRANSPOSE)
            #    pc.factor()
            #    self.transposed=True

            res = self.res
            res_array = res.getArray()
            res_array[:] = d_outputs['u_s']
            tacs.applyBCs(res)
            psi_s = self.psi_s
            gmres.solve(res,psi_s)
            psi_s_array = psi_s.getArray()
            d_residuals['u_s'] = psi_s_array

    def apply_linear(self,inputs,outputs,d_inputs,d_outputs,d_residuals,mode):
        self._update_internal(inputs,outputs)
        if mode == 'fwd':
            if self.check_partials:
                pass
            else:
                raise ValueError('forward mode requested but not implemented')

        if mode == 'rev':
            if 'u_s' in d_residuals:
                tacs = self.tacs

                res  = self.res
                res_array = res.getArray()

                ans  = self.ans
                ans_array = ans.getArray()

                if 'u_s' in d_outputs:

                    ans_array[:] = outputs['u_s']
                    tacs.setVariables(ans)

                    # if nonsymmetric, we need to form the transpose Jacobian
                    #if self._design_vector_changed(inputs['dv_struct']) or not self.transposed:
                    #    alpha = 1.0
                    #    beta  = 0.0
                    #    gamma = 0.0
                    #    tacs.assembleJacobian(alpha,beta,gamma,res,self.mat,matOr=TACS.PY_TRANSPOSE)
                    #    pc.factor()
                    #    self.transposed=True

                    res_array[:] = 0.0

                    self.mat.mult(ans,res)
                    tacs.applyBCs(res)

                    d_outputs['u_s'] += np.array(res_array[:],dtype=float)

                if 'f_s' in d_inputs:
                    res_array[:] = 0.0
                    for i in range(int(inputs['x_s0'].size/3)):
                        for j in range(3):
                            res_array[self.ndof*i+j] = d_residuals['u_s'][3*i+j]
                    # dR/df_s^T = -I
                    tacs.applyBCs(res)
                    d_inputs['f_s'] -= np.array(res_array[:],dtype=float)

                if 'x_s0' in d_inputs:
                    ans_array[:] = d_residuals['u_s']
                    xpt_sens = self.xpt_sens
                    xpt_sens_array = xpt_sens.getArray()

                    tacs.evalAdjointResXptSensProduct(ans, xpt_sens)

                    d_inputs['x_s0'] += np.array(xpt_sens_array[:],dtype=float)

                if 'dv_struct' in d_inputs:
                    adj_res_product  = np.zeros(d_inputs['dv_struct'].size,dtype=TACS.dtype)
                    psi_s_array    = self.psi_s.getArray()
                    psi_s_array[:] = d_residuals['u_s']
                    self.tacs.evalAdjointResProduct(self.psi_s, adj_res_product)

                    # TACS has already done a parallel sum (mpi allreduce) so
                    # only add the product on one rank
                    if self.comm.rank == 0:
                        d_inputs['dv_struct'] +=  np.array(adj_res_product,dtype=float)

    def _design_vector_changed(self,x):
        if self.x_save is None:
            self.x_save = x.copy()
            return True
        elif not np.allclose(x,self.x_save,rtol=1e-10,atol=1e-10):
            self.x_save = x.copy()
            return True
        else:
            return False


class TacsFunctions(ExplicitComponent):
    """
    Component to compute TACS functions

    Assumptions:
        - User will provide a tacs_func_setup function that will set up a list of functions
          => func_list, tacs, struct_ndv = tacs_func_setup(comm)
    """
    def initialize(self):
        self.options.declare('tacs_func_setup', desc = 'function to feed tacs function-evaluation information')

        self.ans = None
        self.tacs = None

        self.mass = False

        self.check_partials = True

    def setup(self):
#        self.set_check_partial_options(wrt='*',directional=True)

        # TACS part of setup
        tacs_func_setup = self.options['tacs_func_setup']
        func_list, tacs, ndv, mat, pc = tacs_func_setup(self.comm)

        self.tacs = tacs
        self.mat = mat
        self.pc = pc

        self.ans = tacs.createVec()
        state_size = self.ans.getArray().size

        self.xpt_sens = tacs.createNodeVec()
        node_size = self.xpt_sens.getArray().size

        s_list = self.comm.allgather(state_size)
        n_list = self.comm.allgather(node_size)
        irank  = self.comm.rank

        s1 = np.sum(s_list[:irank])
        s2 = np.sum(s_list[:irank+1])
        n1 = np.sum(n_list[:irank])
        n2 = np.sum(n_list[:irank+1])

        # OpenMDAO part of setup
        self.add_input('dv_struct', shape=ndv,                                                    desc='tacs design variables')
        self.add_input('x_s0',      shape=node_size,  src_indices=np.arange(n1, n2, dtype=int),   desc='structural node coordinates')
        self.add_input('u_s',       shape=state_size, src_indices=np.arange(s1, s2, dtype=int),   desc='structural state vector')

        # Remove the mass function from the func list if it is there
        # since it is not dependent on the structural state
        func_no_mass = []
        for i,func in enumerate(func_list):
            if isinstance(func,functions.StructuralMass):
                if not self.mass:
                    self.add_output('mass', 0.0, desc = 'structural mass')
                    self.mass = True
            else:
                func_no_mass.append(func)

        self.func_list = func_no_mass
        if len(self.func_list) > 0:
            self.add_output('f_struct', shape=len(self.func_list), desc='structural function values')

            # declare the partials
            #self.declare_partials('f_struct',['dv_struct','x_s0','u_s'])

        if self.mass:
            #self.declare_partials('mass',['dv_struct','x_s0'])
            pass

    def _update_internal(self,inputs):
        self.tacs.setDesignVars(np.array(inputs['dv_struct'],dtype=TACS.dtype))

        xpts = self.tacs.createNodeVec()
        self.tacs.getNodes(xpts)
        xpts_array = xpts.getArray()
        xpts_array[:] = inputs['x_s0']
        self.tacs.setNodes(xpts)

        pc     = self.pc
        alpha = 1.0
        beta  = 0.0
        gamma = 0.0

        xpts = self.tacs.createNodeVec()
        self.tacs.getNodes(xpts)
        xpts_array = xpts.getArray()
        xpts_array[:] = inputs['x_s0']
        self.tacs.setNodes(xpts)

        res = self.tacs.createVec()
        res_array = res.getArray()
        res_array[:] = 0.0

        self.tacs.assembleJacobian(alpha,beta,gamma,res,self.mat)
        pc.factor()

        ans = self.ans
        ans_array = ans.getArray()
        ans_array[:] = inputs['u_s']
        self.tacs.applyBCs(ans)

        self.tacs.setVariables(ans)

    def compute(self,inputs,outputs):
        if self.check_partials:
            self._update_internal(inputs)

        if 'f_struct' in outputs:
            outputs['f_struct'] = self.tacs.evalFunctions(self.func_list)

        if 'mass' in outputs:
            func = functions.StructuralMass(self.tacs)
            outputs['mass'] = self.tacs.evalFunctions([func])

    def compute_jacvec_product(self,inputs, d_inputs, d_outputs, mode):
        if mode == 'fwd':
            if self.check_partials:
                pass
            else:
                raise ValueError('forward mode requested but not implemented')
        if mode == 'rev':
            if self.check_partials:
                self._update_internal(inputs)
            if 'mass' in d_outputs:
                func = functions.StructuralMass(self.tacs)
                if 'dv_struct' in d_inputs:
                    size = d_inputs['dv_struct'].size
                    dvsens = np.zeros(d_inputs['dv_struct'].size,dtype=TACS.dtype)
                    self.tacs.evalDVSens(func, dvsens)

                    d_inputs['dv_struct'] += np.array(dvsens,dtype=float) * d_outputs['mass']

                if 'x_s0' in d_inputs:
                    xpt_sens = self.xpt_sens
                    xpt_sens_array = xpt_sens.getArray()
                    self.tacs.evalXptSens(func, xpt_sens)

                    d_inputs['x_s0'] += np.array(xpt_sens_array,dtype=float) * d_outputs['mass']

            if 'f_struct' in d_outputs:
                for ifunc, func in enumerate(self.func_list):
                    self.tacs.evalFunctions([func])
                    if 'dv_struct' in d_inputs:
                        dvsens = np.zeros(d_inputs['dv_struct'].size,dtype=TACS.dtype)
                        self.tacs.evalDVSens(func, dvsens)

                        d_inputs['dv_struct'][:] += np.array(dvsens,dtype=float) * d_outputs['f_struct'][ifunc]

                    if 'x_s0' in d_inputs:
                        xpt_sens = self.xpt_sens
                        xpt_sens_array = xpt_sens.getArray()
                        self.tacs.evalXptSens(func, xpt_sens)

                        d_inputs['x_s0'][:] += np.array(xpt_sens_array,dtype=float) * d_outputs['f_struct'][ifunc]

                    if 'u_s' in d_inputs:
                        prod = self.tacs.createVec()
                        self.tacs.evalSVSens(func,prod)
                        prod_array = prod.getArray()

                        d_inputs['u_s'][:] += np.array(prod_array,dtype=float) * d_outputs['f_struct'][ifunc]

class PrescribedLoad(ExplicitComponent):
    """
    Prescribe a load to tacs

    Assumptions:
        - User will provide a load_function prescribes the loads
          => load = load_function(x_s0,ndof)
        - User will provide a get_tacs function
          => tacs = get_tacs()
    """
    def initialize(self):
        self.options.declare('load_function', default = None, desc='function that prescribes the loads')
        self.options.declare('get_tacs', default = None, desc='function that gets tacs')

        self.options['distributed'] = True

        self.ndof = 0

    def setup(self):
#        self.set_check_partial_options(wrt='*',directional=True)

        # TACS assembler setup
        tacs = self.options['get_tacs']()

        # create some TACS vectors so we can see what size they are
        xpts  = tacs.createNodeVec()
        node_size = xpts.getArray().size

        tmp   = tacs.createVec()
        state_size = tmp.getArray().size
        self.ndof = int(state_size / ( node_size / 3 ))

        irank = self.comm.rank

        n_list = self.comm.allgather(node_size)
        n1 = np.sum(n_list[:irank])
        n2 = np.sum(n_list[:irank+1])

        # OpenMDAO setup
        self.add_input('x_s0', shape=node_size, src_indices=np.arange(n1, n2, dtype=int), desc='structural node coordinates')
        self.add_output('f_s', shape=state_size, desc='structural load')

        #self.declare_partials('f_s','x_s0')

    def compute(self,inputs,outputs):
        load_function = self.options['load_function']
        outputs['f_s'] = load_function(inputs['x_s0'],self.ndof)

