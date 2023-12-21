# -*- coding: utf-8 -*-

# ===============================================================
# Telegram Tracker
#
# Author: @estebanpdl
#
# File: request_factory.py
# Description: This file contains the RequestFactory class, which 
# serves as a central point for creating instances of different 
# request types (e.g., SearchRequest, ChannelRequest) based on 
# command-line arguments.
# ===============================================================

# Request Factory class
class RequestFactory:
    _request_types = {
        'search': 'This is a search class',
        'channel': 'This is the class for channels',
        'multi_channel': 'This is a class to collect data from multiple channels',
    }

    @staticmethod
    def create_request(req_type, req_input):
        request_class = RequestFactory._request_types.get(req_type)
        if request_class:
            return request_class
        
        raise ValueError(f'Unknown request type: {req_type}')
