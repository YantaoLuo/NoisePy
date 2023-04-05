import gc
import glob
import os
import sys
import time

import numpy as np
import obspy
import pandas as pd
import pyasdf
from mpi4py import MPI
from obspy.clients.fdsn import Client
from scipy.fftpack.helper import next_fast_len

import noise_module

if not sys.warnoptions:
    import warnings

    warnings.simplefilter("ignore")

"""
This script:
    1) downloads sesimic data located in a broad region
    defined by user or using a pre-compiled station list;
    2) cleans up raw traces by removing gaps, instrumental
    response, downsampling and trimming to a day length;
    3) saves data into ASDF format (see Krischer et al.,
    2016 for more details on the data structure);
    4) parallelize the downloading processes with MPI.
    5) avoids downloading data for stations that already
    have 1 or 3 channels
Authors: Chengxin Jiang (chengxin_jiang@fas.harvard.edu)
         Marine Denolle (mdenolle@fas.harvard.edu)
NOTE:
    0. MOST occasions you just need to change parameters followed
    with detailed explanations to run the script.
    1. to avoid segmentation fault later in cross-correlation
    calculations due to too large data in memory,
    a rough estimation of the memory needs is made in the beginning
    of the code. you can reduce the value of
    inc_hours if memory on your machine is not enough to load
    proposed (x) hours of noise data all at once;
    2. if choose to download stations from an existing CSV files,
    stations with the same name but different
    channel is regarded as different stations (same format as those generated by the S0A);
    3. for unknow reasons, including station location code
    during feteching process sometime result in no-data.
    Therefore, we recommend setting location code to "*" in the
    request setting (L105 & 134) when it is confirmed
    manually by the users that no stations with same name
    but different location codes occurs.
Enjoy the NoisePy journey!
"""

#########################################################
# ############## PARAMETER SECTION ######################
#########################################################
tt0 = time.time()

# paths and filenames
rootpath = "./"  # roothpath for the project
direc = os.path.join(rootpath, "RAW_DATA")  # where to store the downloaded data
dlist = os.path.join(direc, "station.txt")  # CSV file for station location info

# download parameters
client = Client("IRIS")  # client/data center. see https://docs.obspy.org/packages/obspy.clients.fdsn.html for a list
down_list = False  # download stations from a pre-compiled list or not
flag = False  # print progress when running the script; recommend to use it at the begining
samp_freq = 2  # targeted sampling rate at X samples per seconds
rm_resp = "no"  # select 'no' to not remove response and use 'inv','spectrum','RESP', or 'polozeros' to remove response
respdir = os.path.join(rootpath, "resp")  # directory where resp files are located (required if rm_resp is neither 'no' nor 'inv')
freqmin = 0.02  # pre filtering frequency bandwidth
freqmax = 1  # note this cannot exceed Nquist freq

# targeted region/station information: only needed when down_list is False
lamin, lamax, lomin, lomax = (
    35.5,
    36.5,
    -120.5,
    -119.5,
)  # regional box: min lat, min lon, max lat, max lon (-114.0)
chan_list = [
    "HHE",
    "HHN",
    "HHZ",
]  # channel if down_list=false (format like "HN?" not work here)
net_list = ["TO"]  # network list
sta_list = ["*"]  # station (using a station list is way either compared to specifying stations one by one)
start_date = ["2015_01_01_0_0_0"]  # start date of download
end_date = ["2015_01_01_12_0_0"]  # end date of download
inc_hours = 12  # length of data for each request (in hour)
ncomp = len(chan_list)

# get rough estimate of memory needs to ensure it now below up in S1
cc_len = 1800  # basic unit of data length for fft (s)
step = 450  # overlapping between each cc_len (s)
MAX_MEM = 5.0  # maximum memory allowed per core in GB

##################################################
# we expect no parameters need to be changed below

# time tags
starttime = obspy.UTCDateTime(start_date[0])
endtime = obspy.UTCDateTime(end_date[0])
if flag:
    print("station.list selected [%s] for data from %s to %s with %sh interval" % (down_list, starttime, endtime, inc_hours))

# assemble parameters used for pre-processing
prepro_para = {
    "rm_resp": rm_resp,
    "respdir": respdir,
    "freqmin": freqmin,
    "freqmax": freqmax,
    "samp_freq": samp_freq,
    "start_date": start_date,
    "end_date": end_date,
    "inc_hours": inc_hours,
    "cc_len": cc_len,
    "step": step,
    "MAX_MEM": MAX_MEM,
    "lamin": lamin,
    "lamax": lamax,
    "lomin": lomin,
    "lomax": lomax,
    "ncomp": ncomp,
}
metadata = os.path.join(direc, "download_info.txt")

# prepare station info (existing station list vs. fetching from client)
if down_list:
    if not os.path.isfile(dlist):
        raise IOError("file %s not exist! double check!" % dlist)

    # read station info from list
    locs = pd.read_csv(dlist)
    nsta = len(locs)
    chan = list(locs.iloc[:]["channel"])
    net = list(locs.iloc[:]["network"])
    sta = list(locs.iloc[:]["station"])
    lat = list(locs.iloc[:]["latitude"])
    lon = list(locs.iloc[:]["longitude"])

    # location info: useful for some occasion
    try:
        location = list(locs.iloc[:]["location"])
    except Exception:
        location = ["*"] * nsta

else:
    # calculate the total number of channels to download
    sta = []
    net = []
    chan = []
    location = []
    lon = []
    lat = []
    elev = []
    nsta = 0

    # loop through specified network, station and channel lists
    for inet in net_list:
        for ista in sta_list:
            for ichan in chan_list:
                # gather station info
                try:
                    inv = client.get_stations(
                        network=inet,
                        station=ista,
                        channel=ichan,
                        location="*",
                        starttime=starttime,
                        endtime=endtime,
                        minlatitude=lamin,
                        maxlatitude=lamax,
                        minlongitude=lomin,
                        maxlongitude=lomax,
                        level="response",
                    )
                except Exception as e:
                    print("Abort at L126 in S0A due to " + str(e))
                    sys.exit()

                for K in inv:
                    for tsta in K:
                        sta.append(tsta.code)
                        net.append(K.code)
                        chan.append(ichan)
                        lon.append(tsta.longitude)
                        lat.append(tsta.latitude)
                        elev.append(tsta.elevation)
                        # sometimes one station has many locations and here we only get the first location
                        if tsta[0].location_code:
                            location.append(tsta[0].location_code)
                        else:
                            location.append("*")
                        nsta += 1
    prepro_para["nsta"] = nsta

# rough estimation on memory needs (assume float32 dtype)
nsec_chunk = inc_hours / 24 * 86400
nseg_chunk = int(np.floor((nsec_chunk - cc_len) / step)) + 1
npts_chunk = int(nseg_chunk * cc_len * samp_freq)
memory_size = nsta * npts_chunk * 4 / 1024**3
if memory_size > MAX_MEM:
    raise ValueError("Require %5.3fG memory but only %5.3fG provided)! Reduce inc_hours to avoid this issue!" % (memory_size, MAX_MEM))


########################################################
# ###############DOWNLOAD SECTION#######################
########################################################

# --------MPI---------
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

if rank == 0:
    if not os.path.isdir(rootpath):
        os.mkdir(rootpath)
    if not os.path.isdir(direc):
        os.mkdir(direc)

    # output station list
    if not down_list:
        dict = {
            "network": net,
            "station": sta,
            "channel": chan,
            "latitude": lat,
            "longitude": lon,
            "elevation": elev,
        }
        locs = pd.DataFrame(dict)
        locs.to_csv(os.path.join(direc, "station.txt"), index=False)

    # save parameters for future reference
    fout = open(metadata, "w")
    fout.write(str(prepro_para))
    fout.close()

    # get MPI variables ready
    all_chunk = noise_module.get_event_list(start_date[0], end_date[0], inc_hours)
    if len(all_chunk) < 1:
        raise ValueError("Abort! no data chunk between %s and %s" % (start_date[0], end_date[0]))
    splits = len(all_chunk) - 1
else:
    splits, all_chunk = [None for _ in range(2)]

# broadcast the variables
splits = comm.bcast(splits, root=0)
all_chunk = comm.bcast(all_chunk, root=0)
extra = splits % size

# MPI: loop through each time chunk
for ick in range(rank, splits, size):
    s1 = obspy.UTCDateTime(all_chunk[ick])
    s2 = obspy.UTCDateTime(all_chunk[ick + 1])
    date_info = {"starttime": s1, "endtime": s2}

    # keep a track of the channels already exists
    num_records = np.zeros(nsta, dtype=np.int16)

    # filename of the ASDF file
    ff = os.path.join(direc, all_chunk[ick] + "T" + all_chunk[ick + 1] + ".h5")
    if not os.path.isfile(ff):
        with pyasdf.ASDFDataSet(ff, mpi=False, compression="gzip-3", mode="w") as ds:
            pass
    else:
        with pyasdf.ASDFDataSet(ff, mpi=False, mode="r") as rds:
            alist = rds.waveforms.list()
            for ista in range(nsta):
                tname = net[ista] + "." + sta[ista]
                if tname in alist:
                    num_records[ista] = len(rds.waveforms[tname].get_waveform_tags())

    # appending when file exists
    with pyasdf.ASDFDataSet(ff, mpi=False, compression="gzip-3", mode="a") as ds:
        # loop through each channel
        for ista in range(nsta):
            # continue when there are alreay data for sta A at day X
            if num_records[ista] == ncomp:
                continue

            # get inventory for specific station
            try:
                sta_inv = client.get_stations(
                    network=net[ista],
                    station=sta[ista],
                    location=location[ista],
                    starttime=s1,
                    endtime=s2,
                    level="response",
                )
            except Exception as e:
                print(e)
                continue

            # add the inventory for all components + all time of this tation
            try:
                ds.add_stationxml(sta_inv)
            except Exception:
                pass

            try:
                # get data
                t0 = time.time()
                tr = client.get_waveforms(
                    network=net[ista],
                    station=sta[ista],
                    channel=chan[ista],
                    location=location[ista],
                    starttime=s1,
                    endtime=s2,
                )
                t1 = time.time()
            except Exception as e:
                print(e, "for", sta[ista])
                continue

            # preprocess to clean data
            print(sta[ista])
            tr = noise_module.preprocess_raw(tr, sta_inv, prepro_para, date_info)
            t2 = time.time()

            if len(tr):
                if location[ista] == "*":
                    tlocation = str("00")
                else:
                    tlocation = location[ista]
                new_tags = "{0:s}_{1:s}".format(chan[ista].lower(), tlocation.lower())
                ds.add_waveforms(tr, tag=new_tags)

            if flag:
                print(ds, new_tags)
                print("downloading data %6.2f s; pre-process %6.2f s" % ((t1 - t0), (t2 - t1)))

tt1 = time.time()
print("downloading step takes %6.2f s" % (tt1 - tt0))

comm.barrier()

rootpath = "./"  # root path for this data processing
CCFDIR = os.path.join(rootpath, "CCF")  # dir to store CC data
DATADIR = os.path.join(rootpath, "RAW_DATA")  # dir where noise data is located
local_data_path = os.path.join(rootpath, "2016_*")

# -------some control parameters--------
input_fmt = "asdf"  # string: 'asdf', 'sac','mseed'
freq_norm = "rma"  # 'no' for no whitening, or 'rma', 'one_bit' for normalization
time_norm = "no"  # 'no' for no normalization, or 'rma', 'one_bit' for normalization
cc_method = "xcorr"  # select between 'raw', 'deconv' and 'coherency'
flag = False  # print intermediate variables and computing time for debugging purpose
acorr_only = False  # only perform auto-correlation
xcorr_only = False  # only perform cross-correlation or not
ncomp = 3  # 1 or 3 component data (needed to decide whether do rotation)

# station/instrument info for input_fmt=='sac' or 'mseed'
stationxml = False  # station.XML file used to remove instrument response for SAC/miniseed data
rm_resp = "no"  # select 'no' to not remove response and use 'inv','spectrum','RESP', or 'polozeros' to remove response
respdir = os.path.join(rootpath, "resp")  # directory where resp files are located (required if rm_resp is neither 'no' nor 'inv')

# pre-processing parameters
cc_len = 1800  # basic unit of data length for fft (sec)
step = 450  # overlapping between each cc_len (sec)
smooth_N = 10  # moving window length for time/freq domain normalization if selected (points)

# cross-correlation parameters
maxlag = 200  # lags of cross-correlation to save (sec)
substack = True  # sub-stack daily cross-correlation or not
substack_len = cc_len  # how long to stack over: need to be multiples of cc_len
smoothspect_N = 10  # moving window length to smooth spectrum amplitude (points)

# criteria for data selection
max_over_std = 10  # threahold to remove window of bad signals
max_kurtosis = 10  # max kurtosis allowed, TO BE ADDED!

# maximum memory allowed per core in GB
MAX_MEM = 4.0

# load useful download info if start from ASDF
if input_fmt == "asdf":
    dfile = os.path.join(DATADIR, "download_info.txt")
    down_info = eval(open(dfile).read())
    samp_freq = down_info["samp_freq"]
    freqmin = down_info["freqmin"]
    freqmax = down_info["freqmax"]
    start_date = down_info["start_date"]
    end_date = down_info["end_date"]
    inc_hours = down_info["inc_hours"]
    # ncomp      = down_info['ncomp']
else:  # sac or mseed format
    samp_freq = 20
    freqmin = 0.05
    freqmax = 4
    start_date = ["2010_12_06_0_0_0"]
    end_date = ["2010_12_15_0_0_0"]
    inc_hours = 12
dt = 1 / samp_freq

##################################################
# we expect no parameters need to be changed below

# make a dictionary to store all variables: also for later cc
fc_para = {
    "samp_freq": samp_freq,
    "dt": dt,
    "cc_len": cc_len,
    "step": step,
    "freqmin": freqmin,
    "freqmax": freqmax,
    "freq_norm": freq_norm,
    "time_norm": time_norm,
    "cc_method": cc_method,
    "smooth_N": smooth_N,
    "data_format": input_fmt,
    "rootpath": rootpath,
    "CCFDIR": CCFDIR,
    "start_date": start_date[0],
    "end_date": end_date[0],
    "inc_hours": inc_hours,
    "substack": substack,
    "substack_len": substack_len,
    "smoothspect_N": smoothspect_N,
    "maxlag": maxlag,
    "max_over_std": max_over_std,
    "MAX_MEM": MAX_MEM,
    "ncomp": ncomp,
    "stationxml": stationxml,
    "rm_resp": rm_resp,
    "respdir": respdir,
    "input_fmt": input_fmt,
}
# save fft metadata for future reference
fc_metadata = os.path.join(CCFDIR, "fft_cc_data.txt")

#######################################
# #########PROCESSING SECTION##########
#######################################

# --------MPI---------
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

if rank == 0:
    if not os.path.isdir(CCFDIR):
        os.mkdir(CCFDIR)

    # save metadata
    fout = open(fc_metadata, "w")
    fout.write(str(fc_para))
    fout.close()

    # set variables to broadcast
    if input_fmt == "asdf":
        tdir = sorted(glob.glob(os.path.join(DATADIR, "*.h5")))
    else:
        tdir = sorted(glob.glob(local_data_path))
        if len(tdir) == 0:
            raise ValueError("No data file in %s", DATADIR)
        # get nsta by loop through all event folder
        nsta = 0
        for ii in range(len(tdir)):
            tnsta = len(glob.glob(os.path.join(tdir[ii], "*" + input_fmt)))
            if nsta < tnsta:
                nsta = tnsta

    nchunk = len(tdir)
    splits = nchunk
    if nchunk == 0:
        raise IOError("Abort! no available seismic files for FFT")
else:
    if input_fmt == "asdf":
        splits, tdir = [None for _ in range(2)]
    else:
        splits, tdir, nsta = [None for _ in range(3)]

# broadcast the variables
splits = comm.bcast(splits, root=0)
tdir = comm.bcast(tdir, root=0)
if input_fmt != "asdf":
    nsta = comm.bcast(nsta, root=0)

# MPI loop: loop through each user-defined time chunk
for ick in range(rank, splits, size):
    t10 = time.time()

    # ###########LOADING NOISE DATA AND DO FFT##################

    # get the tempory file recording cc process
    if input_fmt == "asdf":
        tmpfile = os.path.join(CCFDIR, tdir[ick].split("/")[-1].split(".")[0] + ".tmp")
    else:
        tmpfile = os.path.join(CCFDIR, tdir[ick].split("/")[-1] + ".tmp")

    # check whether time chunk been processed or not
    if os.path.isfile(tmpfile):
        ftemp = open(tmpfile, "r")
        alines = ftemp.readlines()
        if len(alines) and alines[-1] == "done":
            continue
        else:
            ftemp.close()
            os.remove(tmpfile)

    # retrive station information
    if input_fmt == "asdf":
        ds = pyasdf.ASDFDataSet(tdir[ick], mpi=False, mode="r")
        sta_list = ds.waveforms.list()
        nsta = ncomp * len(sta_list)
        print("found %d stations in total" % nsta)
    else:
        sta_list = sorted(glob.glob(os.path.join(tdir[ick], "*" + input_fmt)))
    if len(sta_list) == 0:
        print("continue! no data in %s" % tdir[ick])
        continue

    # crude estimation on memory needs (assume float32)
    nsec_chunk = inc_hours / 24 * 86400
    nseg_chunk = int(np.floor((nsec_chunk - cc_len) / step))
    npts_chunk = int(nseg_chunk * cc_len * samp_freq)
    memory_size = nsta * npts_chunk * 4 / 1024**3
    if memory_size > MAX_MEM:
        raise ValueError("Require %5.3fG memory but only %5.3fG provided)! Reduce inc_hours to avoid this issue!" % (memory_size, MAX_MEM))

    nnfft = int(next_fast_len(int(cc_len * samp_freq)))
    # open array to store fft data/info in memory
    fft_array = np.zeros((nsta, nseg_chunk * (nnfft // 2)), dtype=np.complex64)
    fft_std = np.zeros((nsta, nseg_chunk), dtype=np.float32)
    fft_flag = np.zeros(nsta, dtype=np.int16)
    fft_time = np.zeros((nsta, nseg_chunk), dtype=np.float64)
    # station information (for every channel)
    station = []
    network = []
    channel = []
    clon = []
    clat = []
    location = []
    elevation = []

    # loop through all stations
    iii = 0
    for ista in range(len(sta_list)):
        tmps = sta_list[ista]

        if input_fmt == "asdf":
            # get station and inventory
            try:
                inv1 = ds.waveforms[tmps]["StationXML"]
            except Exception:
                print("abort! no stationxml for %s in file %s" % (tmps, tdir[ick]))
                continue
            sta, net, lon, lat, elv, loc = noise_module.sta_info_from_inv(inv1)

            # get days information: works better than just list the tags
            all_tags = ds.waveforms[tmps].get_waveform_tags()
            if len(all_tags) == 0:
                continue

        else:  # get station information
            all_tags = [1]
            sta = tmps.split("/")[-1]

        # ----loop through each stream----
        for itag in range(len(all_tags)):
            if flag:
                print("working on station %s and trace %s" % (sta, all_tags[itag]))

            # read waveform data
            if input_fmt == "asdf":
                source = ds.waveforms[tmps][all_tags[itag]]
            else:
                source = obspy.read(tmps)
                inv1 = noise_module.stats2inv(source[0].stats, fc_para)
                sta, net, lon, lat, elv, loc = noise_module.sta_info_from_inv(inv1)

            comp = source[0].stats.channel
            if comp[-1] == "U":
                comp.replace("U", "Z")
            if len(source) == 0:
                continue

            # cut daily-long data into smaller segments (dataS always in 2D)
            trace_stdS, dataS_t, dataS = noise_module.cut_trace_make_stat(fc_para, source)  # optimized version:3-4 times faster
            if not len(dataS):
                continue
            N = dataS.shape[0]

            # do normalization if needed
            source_white = noise_module.noise_processing(fc_para, dataS)
            Nfft = source_white.shape[1]
            Nfft2 = Nfft // 2
            if flag:
                print("N and Nfft are %d (proposed %d),%d (proposed %d)" % (N, nseg_chunk, Nfft, nnfft))

            # keep track of station info to write into parameter section of ASDF files
            station.append(sta)
            network.append(net)
            channel.append(comp), clon.append(lon)
            clat.append(lat)
            location.append(loc)
            elevation.append(elv)

            # load fft data in memory for cross-correlations
            data = source_white[:, :Nfft2]
            fft_array[iii] = data.reshape(data.size)
            fft_std[iii] = trace_stdS
            fft_flag[iii] = 1
            fft_time[iii] = dataS_t
            iii += 1
            del trace_stdS, dataS_t, dataS, source_white, data

    if input_fmt == "asdf":
        del ds

    # check whether array size is enough
    if iii != nsta:
        print("it seems some stations miss data in download step, but it is OKAY!")

    # ###########PERFORM CROSS-CORRELATION##################
    ftmp = open(tmpfile, "w")
    # make cross-correlations
    for iiS in range(iii):
        fft1 = fft_array[iiS]
        source_std = fft_std[iiS]
        sou_ind = np.where((source_std < fc_para["max_over_std"]) & (source_std > 0) & (np.isnan(source_std) == 0))[0]
        if not fft_flag[iiS] or not len(sou_ind):
            continue

        t0 = time.time()
        # -----------get the smoothed source spectrum for decon later----------
        sfft1 = noise_module.smooth_source_spect(fc_para, fft1)
        sfft1 = sfft1.reshape(N, Nfft2)
        t1 = time.time()
        if flag:
            print("smoothing source takes %6.4fs" % (t1 - t0))

        # get index right for auto/cross correlation
        istart = iiS
        iend = iii
        if acorr_only:
            iend = np.minimum(iiS + 3, iii)
        if xcorr_only:
            istart = np.minimum(iiS + ncomp, iii)

        # -----------now loop III for each receiver B----------
        for iiR in range(istart, iend):
            if flag:
                print("receiver: %s %s" % (station[iiR], network[iiR]))
            if not fft_flag[iiR]:
                continue

            fft2 = fft_array[iiR]
            sfft2 = fft2.reshape(N, Nfft2)
            receiver_std = fft_std[iiR]

            # ---------- check the existence of earthquakes ----------
            rec_ind = np.where((receiver_std < fc_para["max_over_std"]) & (receiver_std > 0) & (np.isnan(receiver_std) == 0))[0]
            bb = np.intersect1d(sou_ind, rec_ind)
            if len(bb) == 0:
                continue

            t2 = time.time()
            corr, tcorr, ncorr = noise_module.correlate(sfft1[bb, :], sfft2[bb, :], fc_para, Nfft, fft_time[iiR][bb])
            t3 = time.time()

            # ---------------keep daily cross-correlation into a hdf5 file--------------
            if input_fmt == "asdf":
                tname = tdir[ick].split("/")[-1]
            else:
                tname = tdir[ick].split("/")[-1] + ".h5"
            cc_h5 = os.path.join(CCFDIR, tname)
            crap = np.zeros(corr.shape, dtype=corr.dtype)

            with pyasdf.ASDFDataSet(cc_h5, mpi=False) as ccf_ds:
                coor = {
                    "lonS": clon[iiS],
                    "latS": clat[iiS],
                    "lonR": clon[iiR],
                    "latR": clat[iiR],
                }
                comp = channel[iiS][-1] + channel[iiR][-1]
                parameters = noise_module.cc_parameters(fc_para, coor, tcorr, ncorr, comp)

                # source-receiver pair
                data_type = network[iiS] + "." + station[iiS] + "_" + network[iiR] + "." + station[iiR]
                path = channel[iiS] + "_" + channel[iiR]
                crap[:] = corr[:]
                ccf_ds.add_auxiliary_data(data=crap, data_type=data_type, path=path, parameters=parameters)
                ftmp.write(
                    network[iiS] + "." + station[iiS] + "." + channel[iiS] + "_" + network[iiR] + "." + station[iiR] + "." + channel[iiR] + "\n"
                )

            t4 = time.time()
            if flag:
                print("read S %6.4fs, cc %6.4fs, write cc %6.4fs" % ((t1 - t0), (t3 - t2), (t4 - t3)))

            del fft2, sfft2, receiver_std
        del fft1, sfft1, source_std

    # create a stamp to show time chunk being done
    ftmp.write("done")
    ftmp.close()

    fft_array = []
    fft_std = []
    fft_flag = []
    fft_time = []
    n = gc.collect()
    print("unreadable garbarge", n)

    t11 = time.time()
    print("it takes %6.2fs to process the chunk of %s" % (t11 - t10, tdir[ick].split("/")[-1]))

tt1 = time.time()
print("it takes %6.2fs to process step 1 in total" % (tt1 - tt0))
comm.barrier()

# absolute path parameters
rootpath = "./"  # root path for this data processing
CCFDIR = os.path.join(rootpath, "CCF")  # dir where CC data is stored
STACKDIR = os.path.join(rootpath, "STACK")  # dir where stacked data is going to
locations = os.path.join(rootpath, "RAW_DATA/station.txt")  # station info including network,station,channel,latitude,longitude,elevation
if not os.path.isfile(locations):
    raise ValueError("Abort! station info is needed for this script")

# define new stacking para
keep_substack = False  # keep all sub-stacks in final ASDF file
flag = False  # output intermediate args for debugging
stack_method = "linear"  # linear, pws, robust, nroot, selective, auto_covariance or all

# new rotation para
rotation = True  # rotation from E-N-Z to R-T-Z
correction = False  # angle correction due to mis-orientation
if rotation and correction:
    corrfile = os.path.join(rootpath, "meso_angles.txt")  # csv file containing angle info to be corrected
    locs = pd.read_csv(corrfile)
else:
    locs = []

# maximum memory allowed per core in GB
MAX_MEM = 4.0

##################################################
# we expect no parameters need to be changed below

# load fc_para parameters from Step1
fc_metadata = os.path.join(CCFDIR, "fft_cc_data.txt")
fc_para = eval(open(fc_metadata).read())
ncomp = fc_para["ncomp"]
samp_freq = fc_para["samp_freq"]
start_date = fc_para["start_date"]
end_date = fc_para["end_date"]
inc_hours = fc_para["inc_hours"]
cc_len = fc_para["cc_len"]
step = fc_para["step"]
maxlag = fc_para["maxlag"]
substack = fc_para["substack"]
substack_len = fc_para["substack_len"]

# cross component info
if ncomp == 1:
    enz_system = ["ZZ"]
else:
    enz_system = ["EE", "EN", "EZ", "NE", "NN", "NZ", "ZE", "ZN", "ZZ"]

rtz_components = ["ZR", "ZT", "ZZ", "RR", "RT", "RZ", "TR", "TT", "TZ"]

# make a dictionary to store all variables: also for later cc
stack_para = {
    "samp_freq": samp_freq,
    "cc_len": cc_len,
    "step": step,
    "rootpath": rootpath,
    "STACKDIR": STACKDIR,
    "start_date": start_date[0],
    "end_date": end_date[0],
    "inc_hours": inc_hours,
    "substack": substack,
    "substack_len": substack_len,
    "maxlag": maxlag,
    "keep_substack": keep_substack,
    "stack_method": stack_method,
    "rotation": rotation,
    "correction": correction,
}
# save fft metadata for future reference
stack_metadata = os.path.join(STACKDIR, "stack_data.txt")

#######################################
# #########PROCESSING SECTION##########
#######################################

# --------MPI---------
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

if rank == 0:
    if not os.path.isdir(STACKDIR):
        os.mkdir(STACKDIR)
    # save metadata
    fout = open(stack_metadata, "w")
    fout.write(str(stack_para))
    fout.close()

    # cross-correlation files
    ccfiles = sorted(glob.glob(os.path.join(CCFDIR, "*.h5")))

    # load station info
    tlocs = pd.read_csv(locations)
    sta = sorted(np.unique(tlocs["network"] + "." + tlocs["station"]))
    for ii in range(len(sta)):
        tmp = os.path.join(STACKDIR, sta[ii])
        if not os.path.isdir(tmp):
            os.mkdir(tmp)

    # station-pairs
    pairs_all = []
    for ii in range(len(sta) - 1):
        for jj in range(ii, len(sta)):
            pairs_all.append(sta[ii] + "_" + sta[jj])

    splits = len(pairs_all)
    if len(ccfiles) == 0 or splits == 0:
        raise IOError("Abort! no available CCF data for stacking")

else:
    splits, ccfiles, pairs_all = [None for _ in range(3)]

# broadcast the variables
splits = comm.bcast(splits, root=0)
ccfiles = comm.bcast(ccfiles, root=0)
pairs_all = comm.bcast(pairs_all, root=0)

# MPI loop: loop through each user-defined time chunck
for ipair in range(rank, splits, size):
    t0 = time.time()

    if flag:
        print("%dth path for station-pair %s" % (ipair, pairs_all[ipair]))
    # source folder
    ttr = pairs_all[ipair].split("_")
    snet, ssta = ttr[0].split(".")
    rnet, rsta = ttr[1].split(".")
    idir = ttr[0]

    # continue when file is done
    toutfn = os.path.join(STACKDIR, idir + "/" + pairs_all[ipair] + ".tmp")
    if os.path.isfile(toutfn):
        continue

    # crude estimation on memory needs (assume float32)
    nccomp = ncomp * ncomp
    num_chunck = len(ccfiles) * nccomp
    num_segmts = 1
    if substack:  # things are difference when do substack
        if substack_len == cc_len:
            num_segmts = int(np.floor((inc_hours * 3600 - cc_len) / step))
        else:
            num_segmts = int(inc_hours / (substack_len / 3600))
    npts_segmt = int(2 * maxlag * samp_freq) + 1
    memory_size = num_chunck * num_segmts * npts_segmt * 4 / 1024**3

    if memory_size > MAX_MEM:
        raise ValueError("Require %5.3fG memory but only %5.3fG provided)! Cannot load cc data all once!" % (memory_size, MAX_MEM))
    if flag:
        print("Good on memory (need %5.2f G and %s G provided)!" % (memory_size, MAX_MEM))

    # allocate array to store fft data/info
    cc_array = np.zeros((num_chunck * num_segmts, npts_segmt), dtype=np.float32)
    cc_time = np.zeros(num_chunck * num_segmts, dtype=np.float)
    cc_ngood = np.zeros(num_chunck * num_segmts, dtype=np.int16)
    cc_comp = np.chararray(num_chunck * num_segmts, itemsize=2, unicode=True)

    # loop through all time-chuncks
    iseg = 0
    dtype = pairs_all[ipair]
    for ifile in ccfiles:
        # load the data from daily compilation
        ds = pyasdf.ASDFDataSet(ifile, mpi=False, mode="r")
        try:
            path_list = ds.auxiliary_data[dtype].list()
            tparameters = ds.auxiliary_data[dtype][path_list[0]].parameters
        except Exception:
            if flag:
                print("continue! no pair of %s in %s" % (dtype, ifile))
            continue

        if ncomp == 3 and len(path_list) < 9:
            if flag:
                print("continue! not enough cross components for %s in %s" % (dtype, ifile))
            continue

        if len(path_list) > 9:
            raise ValueError("more than 9 cross-component exists for %s %s! please double check" % (ifile, dtype))

        # load the 9-component data, which is in order in the ASDF
        for tpath in path_list:
            cmp1 = tpath.split("_")[0]
            cmp2 = tpath.split("_")[1]
            tcmp1 = cmp1[-1]
            tcmp2 = cmp2[-1]

            # read data and parameter matrix
            tdata = ds.auxiliary_data[dtype][tpath].data[:]
            ttime = ds.auxiliary_data[dtype][tpath].parameters["time"]
            tgood = ds.auxiliary_data[dtype][tpath].parameters["ngood"]
            if substack:
                for ii in range(tdata.shape[0]):
                    cc_array[iseg] = tdata[ii]
                    cc_time[iseg] = ttime[ii]
                    cc_ngood[iseg] = tgood[ii]
                    cc_comp[iseg] = tcmp1 + tcmp2
                    iseg += 1
            else:
                cc_array[iseg] = tdata
                cc_time[iseg] = ttime
                cc_ngood[iseg] = tgood
                cc_comp[iseg] = tcmp1 + tcmp2
                iseg += 1

    t1 = time.time()
    if flag:
        print("loading CCF data takes %6.2fs" % (t1 - t0))

    # continue when there is no data
    if iseg <= 1:
        continue
    outfn = pairs_all[ipair] + ".h5"
    if flag:
        print("ready to output to %s" % (outfn))

    # matrix used for rotation
    if rotation:
        bigstack = np.zeros(shape=(9, npts_segmt), dtype=np.float32)
    if stack_method == "all":
        bigstack1 = np.zeros(shape=(9, npts_segmt), dtype=np.float32)
        bigstack2 = np.zeros(shape=(9, npts_segmt), dtype=np.float32)

    # loop through cross-component for stacking
    iflag = 1
    for icomp in range(nccomp):
        comp = enz_system[icomp]
        indx = np.where(cc_comp == comp)[0]

        # jump if there are not enough data
        if len(indx) < 2:
            iflag = 0
            break

        t2 = time.time()
        stack_h5 = os.path.join(STACKDIR, idir + "/" + outfn)
        # output stacked data
        (
            cc_final,
            ngood_final,
            stamps_final,
            allstacks1,
            allstacks2,
            allstacks3,
            nstacks,
        ) = noise_module.stacking(cc_array[indx], cc_time[indx], cc_ngood[indx], stack_para)
        if not len(allstacks1):
            continue
        if rotation:
            bigstack[icomp] = allstacks1
            if stack_method == "all":
                bigstack1[icomp] = allstacks2
                bigstack2[icomp] = allstacks3

        # write stacked data into ASDF file
        with pyasdf.ASDFDataSet(stack_h5, mpi=False) as ds:
            tparameters["time"] = stamps_final[0]
            tparameters["ngood"] = nstacks
            if stack_method != "all":
                data_type = "Allstack_" + stack_method
                ds.add_auxiliary_data(
                    data=allstacks1,
                    data_type=data_type,
                    path=comp,
                    parameters=tparameters,
                )
            else:
                ds.add_auxiliary_data(
                    data=allstacks1,
                    data_type="Allstack_linear",
                    path=comp,
                    parameters=tparameters,
                )
                ds.add_auxiliary_data(
                    data=allstacks2,
                    data_type="Allstack_pws",
                    path=comp,
                    parameters=tparameters,
                )
                ds.add_auxiliary_data(
                    data=allstacks3,
                    data_type="Allstack_robust",
                    path=comp,
                    parameters=tparameters,
                )

        # keep a track of all sub-stacked data from S1
        if keep_substack:
            for ii in range(cc_final.shape[0]):
                with pyasdf.ASDFDataSet(stack_h5, mpi=False) as ds:
                    tparameters["time"] = stamps_final[ii]
                    tparameters["ngood"] = ngood_final[ii]
                    data_type = "T" + str(int(stamps_final[ii]))
                    ds.add_auxiliary_data(
                        data=cc_final[ii],
                        data_type=data_type,
                        path=comp,
                        parameters=tparameters,
                    )

        t3 = time.time()
        if flag:
            print("takes %6.2fs to stack one component with %s stacking method" % (t3 - t1, stack_method))

    # do rotation if needed
    if rotation and iflag:
        if np.all(bigstack == 0):
            continue
        tparameters["station_source"] = ssta
        tparameters["station_receiver"] = rsta
        if stack_method != "all":
            bigstack_rotated = noise_module.rotation(bigstack, tparameters, locs, flag)

            # write to file
            for icomp in range(nccomp):
                comp = rtz_components[icomp]
                tparameters["time"] = stamps_final[0]
                tparameters["ngood"] = nstacks
                data_type = "Allstack_" + stack_method
                with pyasdf.ASDFDataSet(stack_h5, mpi=False) as ds2:
                    ds2.add_auxiliary_data(
                        data=bigstack_rotated[icomp],
                        data_type=data_type,
                        path=comp,
                        parameters=tparameters,
                    )
        else:
            bigstack_rotated = noise_module.rotation(bigstack, tparameters, locs, flag)
            bigstack_rotated1 = noise_module.rotation(bigstack1, tparameters, locs, flag)
            bigstack_rotated2 = noise_module.rotation(bigstack2, tparameters, locs, flag)

            # write to file
            for icomp in range(nccomp):
                comp = rtz_components[icomp]
                tparameters["time"] = stamps_final[0]
                tparameters["ngood"] = nstacks
                with pyasdf.ASDFDataSet(stack_h5, mpi=False) as ds2:
                    ds2.add_auxiliary_data(
                        data=bigstack_rotated[icomp],
                        data_type="Allstack_linear",
                        path=comp,
                        parameters=tparameters,
                    )
                    ds2.add_auxiliary_data(
                        data=bigstack_rotated1[icomp],
                        data_type="Allstack_pws",
                        path=comp,
                        parameters=tparameters,
                    )
                    ds2.add_auxiliary_data(
                        data=bigstack_rotated2[icomp],
                        data_type="Allstack_robust",
                        path=comp,
                        parameters=tparameters,
                    )

    t4 = time.time()
    if flag:
        print("takes %6.2fs to stack/rotate all station pairs %s" % (t4 - t1, pairs_all[ipair]))

    # write file stamps
    ftmp = open(toutfn, "w")
    ftmp.write("done")
    ftmp.close()

tt1 = time.time()
print("it takes %6.2fs to process step 2 in total" % (tt1 - tt0))
comm.barrier()

# merge all path_array and output
if rank == 0:
    sys.exit()
