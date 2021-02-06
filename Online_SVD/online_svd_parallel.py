import numpy as np
np.random.seed(10)
import matplotlib.pyplot as plt
from mpi4py import MPI

# For shared memory deployment: `export OPENBLAS_NUM_THREADS=1`

# Method of snapshots to accelerate
def generate_right_vectors(Y):
    '''
    Y - Snapshot matrix - shape: NxS
    returns V - truncated right singular vectors
    '''
    new_mat = np.matmul(np.transpose(Y),Y)
    w, v = np.linalg.eig(new_mat)

    svals = np.sqrt(np.abs(w))
    rval = np.argmax(svals<0.0001) # eps0

    return v[:,:rval], np.sqrt(np.abs(w[:rval])) # Covariance eigenvectors, singular values

# Check orthogonality
def check_ortho(modes,num_modes):
    for m1 in range(num_modes):
        for m2 in range(num_modes):
            if m1 == m2:
                s_ = np.sum(modes[:,m1]*modes[:,m2]) 
                if not np.isclose(s_,1.0):
                    print('Orthogonality check failed')
                    break
            else:
                s_ = np.sum(modes[:,m1]*modes[:,m2]) 
                if not np.isclose(s_,0.0):
                    print('Orthogonality check failed')
                    break

    print('Orthogonality check passed successfully')

class online_svd_calculator(object):
    """
    docstring for online_svd_calculator:
    K : Number of modes to truncate
    ff : Forget factor
    """
    def __init__(self, K, ff):
        super(online_svd_calculator, self).__init__()
        self.K = K
        self.ff = ff
        # Initialize MPI
        self.comm = MPI.COMM_WORLD
        self.rank = self.comm.Get_rank()
        self.nprocs = self.comm.Get_size()

        self.iteration = 0

        self.initialize()

    # Initialize
    def initialize(self):        
        A = self.get_new_data(iteration=self.iteration)
        self.ulocal, self.svalue = self.parallel_svd(A)


    def get_new_data(self,iteration):
        return np.load('points_rank_'+str(self.rank)+'_batch_'+str(iteration)+'.npy')

    def parallel_qr(self,A):
        
        # Perform the local QR
        q, r = np.linalg.qr(A)
        rlocal_shape_0 = r.shape[0]
        rlocal_shape_1 = r.shape[1]

        # Gather data at rank 0:
        r_global = self.comm.gather(r,root=0)

        # perform SVD at rank 0:
        if self.rank == 0:
            temp = r_global[0]
            for i in range(self.nprocs-1):
                temp = np.concatenate((temp,r_global[i+1]),axis=0)
            r_global = temp

            qglobal, rfinal = np.linalg.qr(r_global)
            qglobal = -qglobal
            rfinal = -rfinal

            # For this rank
            qlocal = np.matmul(q,qglobal[:rlocal_shape_0])

            # send to other ranks
            for rank in range(1,self.nprocs):
                self.comm.send(qglobal[rank*rlocal_shape_0:(rank+1)*rlocal_shape_0], dest=rank, tag=rank+10)

            # Step b of Levy-Lindenbaum - small operation
            unew, snew, _ = np.linalg.svd(rfinal)
            unew = unew[:,:self.K]
            snew = snew[:self.K]

        else:
            # Receive qglobal slices from other ranks
            qglobal = self.comm.recv(source=0, tag=self.rank+10)

            # For this rank
            qlocal = np.matmul(q,qglobal)

            # To receive new singular vectors
            unew = None
            snew = None

        unew = self.comm.bcast(unew,root=0)
        snew = self.comm.bcast(snew,root=0)

        return qlocal, unew, snew

    def parallel_svd(self,A):

        vlocal, slocal = generate_right_vectors(A)

        # Find Wr
        wlocal = np.matmul(vlocal,np.diag(slocal).T)

        # Gather data at rank 0:
        wglobal = self.comm.gather(wlocal,root=0)

        # perform SVD at rank 0:
        if self.rank == 0:
            temp = wglobal[0]
            for i in range(self.nprocs-1):
                temp = np.concatenate((temp,wglobal[i+1]),axis=-1)
            wglobal = temp

            x, s, y = np.linalg.svd(wglobal)
        else:
            x = None
            s = None
        
        x = self.comm.bcast(x,root=0)
        s = self.comm.bcast(s,root=0)

        # # Find truncation threshold
        # s_ratio = np.cumsum(s)/np.sum(s)
        # rval = np.argmax(1.0-s_ratio<0.0001) # eps1

        # perform APMOS at each local rank
        phi_local = []
        for mode in range(self.K):
            phi_temp = 1.0/s[mode]*np.matmul(A,x[:,mode:mode+1])
            phi_local.append(phi_temp)

        temp = phi_local[0]
        for i in range(self.K-1):
            temp = np.concatenate((temp,phi_local[i+1]),axis=-1)

        return temp, s[:self.K] #

    def incorporate_data(self):
        self.iteration+=1
        A = self.get_new_data(iteration=self.iteration)

        ll = self.ff*np.matmul(self.ulocal,np.diag(self.svalue))
        ll = np.concatenate((ll,A),axis=-1)

        qlocal, utemp, self.svalue = self.parallel_qr(ll)

        self.ulocal = np.matmul(qlocal,utemp)

    def gather_modes(self):
        # Gather modes at rank 0
        # This is automatically in order
        phi_global = self.comm.gather(self.ulocal,root=0)

        if self.rank == 0:
            phi = phi_global[0]
            for i in range(self.nprocs-1):
                phi = np.concatenate((phi,phi_global[i+1]),axis=0)

            np.save('Online_Parallel_POD.npy',phi)
            np.save('Online_Parallel_SingularValues.npy',self.svalue)

            # Validate
            serial = np.load('Serial_Modes_MOS.npy')
            parallel_online = np.load('Online_Parallel_POD.npy')
            serial_online = np.load('Online_Serial_POD.npy')

            plt.figure()
            plt.plot(serial[:,0],label='serial one-shot')
            plt.plot(parallel_online[:,0],label='parallel_online')
            plt.plot(serial_online[:,0],label='serial_online')
            plt.legend()

            plt.figure()
            plt.plot(serial[:,2],label='serial one-shot')
            plt.plot(parallel_online[:,2],label='parallel_online')
            plt.plot(serial_online[:,2],label='serial_online')
            plt.legend()

            serial_svs = np.load('Serial_SingularValues.npy')
            serial_online_svs = np.load('Online_Serial_SingularValues.npy')
            parallel_online_svs = np.load('Online_Parallel_SingularValues.npy')

            plt.figure()
            plt.plot(serial_svs[:self.K],label='serial one-shot')
            plt.plot(parallel_online_svs[:self.K],label='parallel_online')
            plt.plot(serial_online_svs[:self.K],label='serial_online')
            plt.title('Singular values')
            plt.legend()
            plt.show()

            # Check orthogonality - should all be successful
            check_ortho(serial,self.K)
            check_ortho(serial_online,self.K)
            check_ortho(parallel_online,self.K)




        
if __name__ == '__main__':
    test_class = online_svd_calculator(10,1.0)
    test_class.initialize()

    for iteration in range(3):
        test_class.incorporate_data()

    test_class.gather_modes()