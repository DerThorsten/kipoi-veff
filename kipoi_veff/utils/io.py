from abc import abstractmethod
import six
import numpy as np
from kipoi_veff.utils.generic import prep_str, convert_record, default_vcf_id_gen
from kipoi_veff.parsers import variant_to_dict
from kipoi.data_utils import numpy_collate, numpy_collate_concat
import os
import six
import gzip
import logging
import abc

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def fopen(*args, **kwargs):
    if isinstance(args[0], six.string_types) and args[0].endswith(".gz"):
        return gzip.open(*args, **kwargs)
    else:
        return open(*args, **kwargs)


def recursive_h5_writer(objs, handle, create):
    for key in objs.keys():
        if isinstance(objs[key], dict):
            if create:
                g = handle.create_group(key)
            else:
                g = handle[key]
            recursive_h5_writer(objs[key], g, create)
        else:
            if create:
                max_shape = list(np.array(objs[key]).shape)
                max_shape[0] = None
                handle.create_dataset(name=key, data=np.array(objs[key]), maxshape=tuple(max_shape), chunks=True,
                                      compression='gzip')
            else:
                dset = handle[key]
                n = objs[key]
                dset.resize(dset.shape[0] + n.shape[0], axis=0)
                dset[-(n.shape[0]):] = n


def validate_input(predictions, records, line_ids=None):
    """Validate the input features
    """
    for k in predictions:
        if predictions[k].shape[0] != len(records):
            raise Exception(
                "number of records does not match number the prediction rows for prediction %s." % str(k))

    if line_ids is not None:
        if line_ids.shape[0] != len(records):
            raise Exception("number of line_ids does not match number of VCF records")


def df_to_np_dict(df):
    """Convert DataFrame to numpy dictionary
    """
    return {k: v.values for k, v in six.iteritems(dict(df))}


class BedWriter:
    """
    simple class to save a bed file sequentially
    """

    # At the moment
    def __init__(self, output_fname):
        self.output_fname = output_fname
        self.ofh = open(self.output_fname, "w")

    #

    def append_interval(self, chrom, start, end, id):
        chrom = "chr" + str(chrom).strip("chr")
        self.ofh.write("\t".join([chrom, str(int(start) - 1), str(end), str(id)]) + "\n")

    #

    def close(self):
        self.ofh.close()

    #

    def __enter__(self):
        return self

    #

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class SeqWriter(object):
    __metaclass__ = abc.ABCMeta
    """
    Abstract class for any synchronous writer class that writes DNA sequences generated by the variant
    effect prediction algorithm.
    """

    @abstractmethod
    def __call__(self, model_input_sets):
        """
        Function that will be called by the predict function after every batch
        """
        pass


class SyncHdf5SeqWriter(SeqWriter):
    """
    Write generated ref / alt / ref_rc / alt_rc DNA sequences in an hdf5 file. For small batches this is slow as it has
     to resize the dataset with every call.
    """

    def __init__(self, ouput_fn):
        import h5py
        try:
            os.unlink(ouput_fn)
        except:
            pass
        self.ofh = h5py.File(ouput_fn, "w")
        self._initialised = False

    def __call__(self, model_input_sets):
        model_input_sets_reduced = {}
        for k in ["ref", "alt", "ref_rc", "alt_rc"]:
            if (k in model_input_sets) and (model_input_sets[k] is not None):
                model_input_sets_reduced[k] = model_input_sets[k]
        recursive_h5_writer(model_input_sets_reduced, self.ofh, not self._initialised)
        if not self._initialised:
            self._initialised = True

    def close(self):
        self.ofh.close()


class SyncPredictonsWriter(object):
    __metaclass__ = abc.ABCMeta
    """
    Abstract class for synchronous writers of effect predictions.
    """

    def __init__(self, model):
        self.info_tag_prefix = "KV"
        if (model.name is None) or (model.name == ""):
            self.model_name = model.source_name + ":" + model.info.doc[:15]  # + ":" + model.info.version
        else:
            self.model_name = model.source_name + ":" + model.name.rstrip("/")  # + ":" + str(model.info.version)
            if model.name in [".", "./", "../"]:
                logger.warn("Please consider executing variant effect prediction from a higher directory level "
                            "as your current model name is %s. The model name is used to generate the output"
                            "VCF annotation INFO tag, which will then not be informative." % (model.name))

        if (self.model_name is not None) or (self.model_name != ""):
            self.info_tag_prefix += ":%s" % prep_str(self.model_name).lstrip("_").rstrip("_")

    @abstractmethod
    def __call__(self, predictions, records):
        """
        Function that will be called by the predict function after every batch
        """
        pass


class VcfWriterCyvcf2(SyncPredictonsWriter):
    """
    Synchronous writer for output VCF
    The reference cyvcf2 object here has to be the one from which the records are taken. INFO tags of this reference
    object will be modified in the process! Hence use carefully!
    """

    def __init__(self, model, reference_cyvcf2_obj, out_vcf_fpath, id_delim=":", vcf_id_generator=default_vcf_id_gen,
                 standardise_var_id=False):
        super(VcfWriterCyvcf2, self).__init__(model)
        # self.vcf_reader = cyvcf2.Reader(reference_vcf_path, "r")
        self.vcf_reader = reference_cyvcf2_obj
        self.out_vcf_fpath = out_vcf_fpath
        self.id_delim = id_delim
        self.prediction_labels = None
        self.column_labels = None
        self.vcf_id_generator = vcf_id_generator
        self.vcf_writer = None
        self.standardise_var_id = standardise_var_id

    def __call__(self, predictions, records, line_ids=None):
        # First itertation: the output file has to be created and the headers defined
        import cyvcf2

        if len(predictions) == 0:
            return None

        metdata_id_infotag = self.info_tag_prefix + ":rID"

        if self.prediction_labels is None:
            # setup the header
            self.prediction_labels = list(predictions.keys())
            for k in predictions:
                col_labels_here = predictions[k].columns.tolist()
                # Make sure that the column are consistent across different prediction methods
                if self.column_labels is None:
                    self.column_labels = col_labels_here
                else:
                    if not np.all(np.array(self.column_labels) == np.array(col_labels_here)):
                        raise Exception(
                            "Prediction columns are not identical for methods %s and %s" % (predictions.keys()[0], k))
                # Add the tag to the vcf file
                # "##INFO=<ID={ID},Number={Number},Type={Type},Description=\"{Description}\">".format(**adict)
                info_tag = {"ID": self.info_tag_prefix + ":%s" % k.upper(),
                            "Number": None, "Type": "String",
                            "Description": "%s SNV effect prediction. Prediction from model outputs: %s" % (
                                k.upper(), "|".join(self.column_labels))}
                self.vcf_reader.add_info_to_header(info_tag)
            # Add a tag in which the line_id = ranges_id will be written
            info_tag = {"ID": metdata_id_infotag,
                        "Number": None, "Type": "String",
                        "Description": "Range or region id taken from metadata, generated by the DataLoader."}
            self.vcf_reader.add_info_to_header(info_tag)
            # Now we can also create the vcf writer
            self.vcf_writer = cyvcf2.Writer(self.out_vcf_fpath, self.vcf_reader)
        else:
            if (len(predictions) != len(self.prediction_labels)) or not all(
                    [k in predictions for k in self.prediction_labels]):
                raise Exception("Predictions are not consistent across batches")
            for k in predictions:
                col_labels_here = predictions[k].columns.tolist()
                if not np.all(np.array(self.column_labels) == np.array(col_labels_here)):
                    raise Exception(
                        "Prediction columns are not identical for methods %s and %s" % (self.prediction_labels[0], k))

        # sanity check that the number of records matches the prediction rows:
        validate_input(predictions, records, line_ids)

        # Actually write the vcf entries.
        for pred_line, record in enumerate(records):
            if self.standardise_var_id and self.vcf_id_generator is not None:
                record.ID = self.vcf_id_generator(record)
            for k in predictions:
                # In case there is a pediction for this line, annotate the vcf...
                preds = predictions[k].iloc[pred_line, :]
                info_tag = self.info_tag_prefix + ":{0}".format(k.upper())
                record.INFO[info_tag] = "|".join([str(pred) for pred in preds])
            line_id = ""
            if line_ids is not None:
                line_id = line_ids[pred_line]
            record.INFO[metdata_id_infotag] = line_id
            self.vcf_writer.write_record(record)

    def close(self):
        if self.vcf_writer is not None:
            self.vcf_writer.close()


class VcfWriter(SyncPredictonsWriter):
    """
    Synchronous writer for output VCF
    This version uses PyVCF and converts cyvcf2 records into PyVCF ones prior to writing. Here just make sure that
    the VCF file used here is identical to the one used in cyvcf2.
    """

    def __init__(self, model, reference_vcf_path, out_vcf_fpath, id_delim=":", vcf_id_generator=default_vcf_id_gen,
                 standardise_var_id=False):
        import vcf
        super(VcfWriter, self).__init__(model)
        compressed = reference_vcf_path.endswith(".gz")
        self.vcf_reader = vcf.Reader(filename=reference_vcf_path, compressed=compressed)
        # self.vcf_reader = reference_vcf_obj
        self.out_vcf_fpath = out_vcf_fpath
        self.id_delim = id_delim
        self.prediction_labels = None
        self.column_labels = None
        self.vcf_id_generator = vcf_id_generator
        self.vcf_writer = None
        self.standardise_var_id = standardise_var_id

    @staticmethod
    def _generate_info_field(id, num, info_type, desc, source, version):
        import vcf
        return vcf.parser._Info(id, num,
                                info_type, desc,
                                source, version)

    def __call__(self, predictions, records, line_ids=None):
        # First itertation: the output file has to be created and the headers defined
        import vcf
        if len(predictions) == 0:
            return None

        metdata_id_infotag = self.info_tag_prefix + ":rID"

        if self.prediction_labels is None:
            # setup the header
            self.prediction_labels = list(predictions.keys())
            for k in predictions:
                col_labels_here = predictions[k].columns.tolist()
                # Make sure that the column are consistent across different prediction methods
                if self.column_labels is None:
                    self.column_labels = col_labels_here
                else:
                    if not np.all(np.array(self.column_labels) == np.array(col_labels_here)):
                        raise Exception(
                            "Prediction columns are not identical for methods %s and %s" % (predictions.keys()[0], k))
                # Add the tag to the vcf file
                info_tag = self.info_tag_prefix + ":%s" % k.upper()
                self.vcf_reader.infos[info_tag] = self._generate_info_field(info_tag, None, 'String',
                                                                            "%s SNV effect prediction. Prediction from model outputs: %s" % (
                                                                                k.upper(),
                                                                                "|".join(self.column_labels)),
                                                                            None, None)
            # Add a tag in which the line_id = ranges_id will be written
            self.vcf_reader.infos[metdata_id_infotag] = self._generate_info_field(metdata_id_infotag, None, 'String',
                                                                                  "Range or region id taken from metadata, "
                                                                                  "generated by the DataLoader.",
                                                                                  None, None)
            # Now we can also create the vcf writer
            self.vcf_writer = vcf.Writer(open(self.out_vcf_fpath, 'w'), self.vcf_reader)
        else:
            if (len(predictions) != len(self.prediction_labels)) or (
                    not all([k in predictions for k in self.prediction_labels])):
                raise Exception("Predictions are not consistent across batches")
            for k in predictions:
                col_labels_here = predictions[k].columns.tolist()
                if not np.all(np.array(self.column_labels) == np.array(col_labels_here)):
                    raise Exception(
                        "Prediction columns are not identical for methods %s and %s" % (self.prediction_labels[0], k))

        # sanity check that the number of records matches the prediction rows:
        validate_input(predictions, records, line_ids)

        # Actually write the vcf entries.
        for pred_line, record in enumerate(records):
            record_vcf = convert_record(record, self.vcf_reader)
            if self.standardise_var_id and self.vcf_id_generator is not None:
                record_vcf.ID = self.vcf_id_generator(record)
            for k in predictions:
                # In case there is a pediction for this line, annotate the vcf...
                preds = predictions[k].iloc[pred_line, :]
                info_tag = self.info_tag_prefix + ":{0}".format(k.upper())
                record_vcf.INFO[info_tag] = "|".join([str("%.8f" % pred) for pred in preds])
            line_id = ""
            if line_ids is not None:
                line_id = line_ids[pred_line]
            record_vcf.INFO[metdata_id_infotag] = line_id
            self.vcf_writer.write_record(record_vcf)

    def close(self):
        if self.vcf_writer is not None:
            self.vcf_writer.close()


class SyncBatchWriter(SyncPredictonsWriter):
    """Use batch writer from Kipoi to write the predictions to file

    # Arguments
      batch_writer: kipoi.writers.BatchWriter
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer

    def __call__(self, predictions, records, line_ids=None):
        validate_input(predictions, records, line_ids)

        if line_ids is None:
            line_ids = {}

        batch = numpy_collate([variant_to_dict(v) for v in records])
        batch['line_idx'] = np.array(line_ids)
        batch['preds'] = {k: df_to_np_dict(df) for k, df in six.iteritems(predictions)}

        self.batch_writer.batch_write(batch)

    def close(self):
        self.batch_writer.close()
