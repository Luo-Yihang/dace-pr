#!/usr/bin/env python3

import numpy as np

import argparse
import scipy
import random

import dace
from dace.memlet import Memlet

import dace.libraries.blas as blas
import dace.libraries.blas.utility.fpga_helper as streaming
from dace.libraries.blas.utility import memory_operations as memOps
from dace.transformation.interstate import GPUTransformSDFG

from dace.libraries.blas.utility.memory_operations import aligned_ndarray

from multiprocessing import Process, Queue


def run_program(program, a, b, c, alpha, testN, ref_result, queue):

    program(x1=a, y1=b, a=alpha, z1=c, n=np.int32(testN))
    ref_norm = np.linalg.norm(c - ref_result) / testN

    queue.put(ref_norm)



def run_test(configs, target, implementation, overwrite_y=False):

    testN = int(2**13)

    for config in configs:

        prec = np.float32 if config[2] == dace.float32 else np.float64
        a = aligned_ndarray(np.random.uniform(0, 100, testN).astype(prec), alignment=256)
        b = aligned_ndarray(np.random.uniform(0, 100, testN).astype(prec), alignment=256)
        b_ref = b.copy()

        c = aligned_ndarray(np.zeros(testN).astype(prec), alignment=256)
        alpha = np.float32(config[0]) if config[2] == dace.float32 else np.float64(config[0])

        ref_result = reference_result(a, b_ref, alpha)

        program = None
        if target == "cpu":
            program = cpu_graph(config[2], implementation, testCase=config[3])
        elif target == "fpga":
            program = fpga_graph(config[1], config[2], implementation, testCase=config[3])
        else:
            program = pure_graph(config[1], config[2], testCase=config[3])

        ref_norm = 0
        if target == "fpga":

            # Run FPGA tests in a different process to avoid issues with Intel OpenCL tools
            queue = Queue()
            p = Process(target=run_program, args=(program, a, b, c, alpha, testN, ref_result, queue))
            p.start()
            p.join()
            ref_norm = queue.get()

        elif overwrite_y:
            program(x1=a, y1=b, a=alpha, z1=b, n=np.int32(testN))
            ref_norm = np.linalg.norm(b - ref_result) / testN
        else:
            program(x1=a, y1=b, a=alpha, z1=c, n=np.int32(testN))
            ref_norm = np.linalg.norm(c - ref_result) / testN

        passed = ref_norm < 1e-5

        if not passed:
            raise RuntimeError('AXPY pure implementation wrong test results on config: ', config)



# ---------- ----------
# Ref result
# ---------- ----------
def reference_result(x_in, y_in, alpha):
    return scipy.linalg.blas.saxpy(x_in, y_in, a=alpha)


# ---------- ----------
# Pure graph program
# ---------- ----------
def pure_graph(vecWidth, precision, implementation="pure", testCase="0"):
    
    n = dace.symbol("n")
    a = dace.symbol("a")

    prec = "single" if precision == dace.float32 else "double"
    test_sdfg = dace.SDFG("axpy_test_" + prec + "_v" + str(vecWidth) + "_" + implementation + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    vecType = dace.vector(precision, vecWidth)

    test_sdfg.add_symbol(a.name, precision)

    test_sdfg.add_array('x1', shape=[n/vecWidth], dtype=vecType)
    test_sdfg.add_array('y1', shape=[n/vecWidth], dtype=vecType)
    test_sdfg.add_array('z1', shape=[n/vecWidth], dtype=vecType)

    x_in = test_state.add_read('x1')
    y_in = test_state.add_read('y1')
    z_out = test_state.add_write('z1')

    saxpy_node = blas.axpy.Axpy("axpy", precision, vecWidth=vecWidth)
    saxpy_node.implementation = implementation

    test_state.add_memlet_path(
        x_in, saxpy_node,
        dst_conn='_x',
        memlet=Memlet.simple(x_in, "0:n/{}".format(vecWidth))
    )
    test_state.add_memlet_path(
        y_in, saxpy_node,
        dst_conn='_y',
        memlet=Memlet.simple(y_in, "0:n/{}".format(vecWidth))
    )

    test_state.add_memlet_path(
        saxpy_node, z_out,
        src_conn='_res',
        memlet=Memlet.simple(z_out, "0:n/{}".format(vecWidth))
    )

    test_sdfg.expand_library_nodes()

    return test_sdfg.compile()


def test_pure():

    print("Run BLAS test: AXPY pure...")

    configs = [
        (1.0, 1, dace.float32, "0"),
        (0.0, 1, dace.float32, "1"),
        (random.random(), 1, dace.float32, "2"),
        (1.0, 1, dace.float64, "3"),
        (1.0, 4, dace.float64, "4")
    ]

    run_test(configs, "pure", "pure")

    print(" --> passed")


# ---------- ----------
# CPU library graph program
# ---------- ----------
def cpu_graph(precision, implementation, testCase="0"):
    
    n = dace.symbol("n")
    a = dace.symbol("a")

    prec = "single" if precision == dace.float32 else "double"
    test_sdfg = dace.SDFG("axpy_test_" + prec + "_" + implementation + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_symbol(a.name, precision)

    test_sdfg.add_array('x1', shape=[n], dtype=precision)
    test_sdfg.add_array('y1', shape=[n], dtype=precision)

    x_in = test_state.add_read('x1')
    y_in = test_state.add_read('y1')
    z_out = test_state.add_write('y1')

    saxpy_node = blas.axpy.Axpy("axpy", precision)
    saxpy_node.implementation = implementation

    test_state.add_memlet_path(
        x_in, saxpy_node,
        dst_conn='_x',
        memlet=Memlet.simple(x_in, "0:n")
    )
    test_state.add_memlet_path(
        y_in, saxpy_node,
        dst_conn='_y',
        memlet=Memlet.simple(y_in, "0:n")
    )

    test_state.add_memlet_path(
        saxpy_node, z_out,
        src_conn='_res',
        memlet=Memlet.simple(z_out, "0:n")
    )


    test_sdfg.expand_library_nodes()

    return test_sdfg.compile()


def test_cpu(implementation):
    
    print("Run BLAS test: AXPY", implementation + "...")

    configs = [
        (1.0, 1, dace.float32, "0"),
        (0.0, 1, dace.float32, "1"),
        (random.random(), 1, dace.float32, "2"),
        (1.0, 1, dace.float64, "3")
    ]

    run_test(configs, "cpu", implementation, overwrite_y=True)

    print(" --> passed")


# ---------- ----------
# FPGA graph program
# ---------- ----------
def fpga_graph(vecWidth, precision, vendor, testCase="0"):
    
    DATATYPE = precision

    n = dace.symbol("n")
    a = dace.symbol("a")

    vendor_mark = "x" if vendor == "xilinx" else "i"
    test_sdfg = dace.SDFG("axpy_test_" + vendor_mark + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")
    
    vecType = dace.vector(precision, vecWidth)

    test_sdfg.add_symbol(a.name, DATATYPE)

    test_sdfg.add_array('x1', shape=[n/vecWidth], dtype=vecType)
    test_sdfg.add_array('y1', shape=[n/vecWidth], dtype=vecType)
    test_sdfg.add_array('z1', shape=[n/vecWidth], dtype=vecType)

    saxpy_node = blas.axpy.Axpy("axpy", DATATYPE , vecWidth=vecWidth, n=n, a=a)
    saxpy_node.implementation = 'fpga_stream'

    x_stream = streaming.streamReadVector(
        'x1',
        n,
        DATATYPE,
        vecWidth=vecWidth
    )

    y_stream = streaming.streamReadVector(
        'y1',
        n,
        DATATYPE,
        vecWidth=vecWidth
    )

    z_stream = streaming.streamWriteVector(
        'z1',
        n,
        DATATYPE,
        vecWidth=vecWidth
    )

    preState, postState = streaming.fpga_setup_connect_streamers(
        test_sdfg,
        test_state,
        saxpy_node,
        [x_stream, y_stream],
        ['_x', '_y'],
        saxpy_node,
        [z_stream],
        ['_res'],
        inputMemoryBanks=[0, 1],
        outputMemoryBanks=[2]
    )

    test_sdfg.expand_library_nodes()

    mode = "simulation" if vendor == "xilinx" else "emulator"
    dace.config.Config.set("compiler", "fpga_vendor", value=vendor)
    dace.config.Config.set("compiler", vendor, "mode", value=mode)

    return test_sdfg.compile()


def test_fpga(vendor):
    
    print("Run BLAS test: AXPY fpga", vendor + "...")

    configs = [
        (0.0, 1, dace.float32, "0"),
        (1.0, 1, dace.float32, "1"),
        (random.random(), 1, dace.float32, "2"),
        (1.0, 1, dace.float64, "3"),
        (1.0, 4, dace.float64, "4")
    ]

    run_test(configs, "fpga", vendor)

    print(" --> passed")


if __name__ == "__main__":

    cmdParser = argparse.ArgumentParser(allow_abbrev=False)

    cmdParser.add_argument("--target", dest="target", default="pure")

    args = cmdParser.parse_args()
    
    if args.target == "MKL" or args.target == "OpenBLAS":
        test_cpu(args.target)
    elif args.target == "intel_fpga" or args.target == "xilinx":
        test_fpga(args.target)
    else:
        test_pure()

        
