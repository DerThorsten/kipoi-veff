import sys
import pytest
import os
import subprocess
import config

if config.install_req:
    INSTALL_FLAG = "--install_req"
else:
    INSTALL_FLAG = ""

EXAMPLES_TO_RUN = ["rbp"]


@pytest.mark.parametrize("example", EXAMPLES_TO_RUN)
@pytest.mark.parametrize("new_dataloader_kwargs_format", [False, True])
def test_generate_mutation_maps_example(example, new_dataloader_kwargs_format, tmpdir):
    """kipoi predict ...
    """
    if (example not in {"rbp"}) or (sys.version_info[0] == 2):
        pytest.skip("Only rbp example testable at the moment, which only runs on py3")

    example_dir = "tests/models/{0}/".format(example)

    tmpdir_here = tmpdir.mkdir("example")

    # restricted_bed = False
    mm_tmpfile = str(tmpdir_here.join("out_mm.hdf5"))
    plt_tmpfile = str(tmpdir_here.join("plot.png"))

    dataloader_kwargs = {"fasta_file": "example_files/hg38_chr22.fa",
                             "preproc_transformer": "dataloader_files/encodeSplines.pkl",
                             "gtf_file": "example_files/gencode_v25_chr22.gtf.pkl.gz",
                             "intervals_file": "example_files/variant_intervals.tsv"}
    dataloader_kwargs = {k: example_dir + v for k, v in dataloader_kwargs.items()}    

    if not new_dataloader_kwargs_format:
        
        import json
        dataloader_kwargs_str = json.dumps(dataloader_kwargs)
        args = ["python", os.path.abspath("./kipoi_veff/cli.py"),
            "create_mutation_map",
            # "./",  # directory
            example_dir,
            "--source=dir",
            "--batch_size=4",
            "--dataloader_args='%s'" % dataloader_kwargs_str,
            "--regions_file", example_dir + "example_files/first_variant.vcf",
            "--output", mm_tmpfile]
    else:
        dataloader_kwargs_list = ["{0}={1}".format(key, val) for key,val in dataloader_kwargs.items()]

        args = ["python", os.path.abspath("./kipoi_veff/cli.py"),
                "create_mutation_map",
                # "./",  # directory
                example_dir,
                "--source=dir",
                "--batch_size=4",
                "--dataloader_args"] + dataloader_kwargs_list + ["--regions_file", example_dir + "example_files/first_variant.vcf",
                "--output", mm_tmpfile]
    # run the
    if INSTALL_FLAG:
        args.append(INSTALL_FLAG)

    returncode = subprocess.call(args=args, cwd=".")
    assert returncode == 0
    assert os.path.exists(mm_tmpfile)

    # make the plot
    args = ["python", os.path.abspath("./kipoi_veff/cli.py"),
            "plot_mutation_map",
            "--input_file=" + mm_tmpfile,
            "--input_entry=0",
            "--model_seq_input=seq",
            "--scoring_key=diff",
            "--model_output=rbp_prb",
            "--limit_region_genomic", "21541588", "21541592",
            "--rc_plot",
            "--output", plt_tmpfile]

    returncode = subprocess.call(args=args,
                                 cwd=os.path.realpath(example_dir))
    assert returncode == 0

    assert os.path.exists(plt_tmpfile)

    os.unlink(mm_tmpfile)
    os.unlink(plt_tmpfile)
